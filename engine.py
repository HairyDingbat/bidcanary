"""Bid Canary alert engine (v0.2, free-tier stack).

Pulls new notices from the free Find a Tender OCDS API (Open Government Licence v3.0),
matches them to subscribers, and emails digests:
  * free plan:    weekly digest (Mondays), top matches, no AI summaries
  * starter/pro:  daily digest with plain-English summaries

Storage: Supabase (Postgres over its REST API) in production, SQLite locally/tests.
Payments: Stripe Payment Links; `sync-stripe` reads active subscriptions and sets plans.

Commands (run by GitHub Actions, see .github/workflows):
    python engine.py fetch          # hourly: pull notices, rebuild site/data/counts.json
    python engine.py sync-stripe    # daily: update plans from Stripe
    python engine.py digest         # daily: paid digests; on Mondays also free digests
    python engine.py digest --dry-run
    python engine.py confirmations  # hourly: double opt-in emails

Environment (set as GitHub Actions secrets, never in code):
    SUPABASE_URL, SUPABASE_SERVICE_KEY   production storage (omit both to use SQLite)
    RESEND_API_KEY, FROM_EMAIL           email sending
    STRIPE_SECRET_KEY (restricted, read-only), STRIPE_PRICE_STARTER, STRIPE_PRICE_PRO
    ANTHROPIC_API_KEY                    optional; rule-based summaries without it
    SITE_URL                             e.g. https://bidcanary.co.uk
    POSTAL_ADDRESS                       sender's postal address for the email footer
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

FTS_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
ATTRIBUTION = ("Contains public sector information licensed under the Open Government "
               "Licence v3.0. Source: Find a Tender, Cabinet Office.")
SITE_URL = os.environ.get("SITE_URL", "https://bidcanary.co.uk").rstrip("/")

# CPV prefixes match the code and all its children.
PACKS = {
    "cleaning": {"label": "Cleaning", "cpv": ["909", "3983"], "keywords": ["cleaning", "janitorial", "washroom", "hygiene services"]},
    "security": {"label": "Security", "cpv": ["7971", "5161"], "keywords": ["security services", "guarding", "cctv monitoring", "key holding", "alarm response"]},
    "grounds": {"label": "Grounds maintenance", "cpv": ["7731", "7721"], "keywords": ["grounds maintenance", "landscaping", "grass cutting", "arboricultur"]},
    "fm": {"label": "Facilities management", "cpv": ["7952", "5070"], "keywords": ["facilities management", "soft fm", "hard fm", "caretaking"]},
}
# NUTS/ITL level-1 codes used in Find a Tender delivery addresses.
REGIONS = {
    "UKC": "North East", "UKD": "North West", "UKE": "Yorkshire and the Humber", "UKF": "East Midlands",
    "UKG": "West Midlands", "UKH": "East of England", "UKI": "London", "UKJ": "South East",
    "UKK": "South West", "UKL": "Wales", "UKM": "Scotland", "UKN": "Northern Ireland",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def http_json(url: str, data: bytes | None = None, headers: dict | None = None, method: str | None = None,
              tries: int = 4):
    """JSON request with retries on timeouts, rate limits and server errors."""
    import time
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"User-Agent": "BidCanary/0.2", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(int(e.headers.get("Retry-After") or 5 * (attempt + 1))); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < tries - 1:
                time.sleep(5 * (attempt + 1)); continue
            raise


# ---------------------------------------------------------------- storage

class SQLiteStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS notices (ocid TEXT PRIMARY KEY, title TEXT, description TEXT, buyer TEXT,
      value REAL, deadline TEXT, cpvs TEXT, regions TEXT, url TEXT, summary TEXT, seen_at TEXT);
    CREATE TABLE IF NOT EXISTS subscribers (id TEXT PRIMARY KEY, email TEXT UNIQUE, pack TEXT, regions TEXT,
      plan TEXT DEFAULT 'free', status TEXT DEFAULT 'pending', unsub_token TEXT, created_at TEXT, confirm_sent_at TEXT);
    CREATE TABLE IF NOT EXISTS sent (subscriber_id TEXT, ocid TEXT, sent_at TEXT, PRIMARY KEY (subscriber_id, ocid));
    CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
    """

    def __init__(self, path: str):
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(self.SCHEMA)

    def get_state(self, k):
        r = self.con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def set_state(self, k, v):
        self.con.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, v)); self.con.commit()

    def upsert_notices(self, rows):
        for r in rows:
            self.con.execute(
                """INSERT INTO notices (ocid,title,description,buyer,value,deadline,cpvs,regions,url,seen_at)
                   VALUES (:ocid,:title,:description,:buyer,:value,:deadline,:cpvs,:regions,:url,:seen_at)
                   ON CONFLICT(ocid) DO UPDATE SET title=excluded.title, description=excluded.description,
                   value=excluded.value, deadline=excluded.deadline, cpvs=excluded.cpvs, regions=excluded.regions""",
                {**r, "cpvs": json.dumps(r["cpvs"]), "regions": json.dumps(r["regions"])})
        self.con.commit()

    def open_notices(self):
        today = dt.date.today().isoformat()
        out = []
        for r in self.con.execute("SELECT * FROM notices WHERE deadline IS NULL OR substr(deadline,1,10) >= ?", (today,)):
            d = dict(r); d["cpvs"] = json.loads(d["cpvs"]); d["regions"] = json.loads(d["regions"]); out.append(d)
        return out

    def set_summary(self, ocid, summary):
        self.con.execute("UPDATE notices SET summary=? WHERE ocid=?", (summary, ocid)); self.con.commit()

    def subscribers(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM subscribers WHERE status='active'")]

    def all_subscribers(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM subscribers")]

    def pending_unconfirmed(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM subscribers WHERE status='pending' AND confirm_sent_at IS NULL")]

    def delete_stale_pending(self, before_iso):
        self.con.execute("DELETE FROM subscribers WHERE status='pending' AND created_at < ?", (before_iso,)); self.con.commit()

    def update_subscriber(self, sid, fields):
        sets = ",".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE subscribers SET {sets} WHERE id=?", (*fields.values(), sid)); self.con.commit()

    def insert_subscriber(self, row):
        cols = ",".join(row); q = ",".join("?" * len(row))
        self.con.execute(f"INSERT INTO subscribers ({cols}) VALUES ({q})", tuple(row.values())); self.con.commit()

    def sent_ocids(self, sid):
        return {r["ocid"] for r in self.con.execute("SELECT ocid FROM sent WHERE subscriber_id=?", (sid,))}

    def mark_sent(self, sid, ocids):
        self.con.executemany("INSERT OR IGNORE INTO sent VALUES (?,?,?)", [(sid, o, now()) for o in ocids]); self.con.commit()


class SupabaseStore:
    """Postgres via Supabase's REST API, authenticated with the service key (server-side only)."""

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1/"
        # New-style keys (sb_secret_/sb_publishable_) go in the apikey header only; legacy JWT keys also as Bearer.
        self.h = {"apikey": key, "Content-Type": "application/json"}
        if not key.startswith("sb_"):
            self.h["Authorization"] = f"Bearer {key}"

    def _req(self, method, path, body=None, extra=None):
        data = json.dumps(body).encode() if body is not None else None
        try:
            return http_json(self.base + path, data, {**self.h, **(extra or {})}, method, tries=2)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Supabase {method} {path.split('?')[0]} failed: {e.code} {detail}") from None

    def get_state(self, k):
        r = self._req("GET", f"state?k=eq.{urllib.parse.quote(k)}&select=v")
        return r[0]["v"] if r else None

    def set_state(self, k, v):
        self._req("POST", "state", [{"k": k, "v": v}], {"Prefer": "resolution=merge-duplicates"})

    def upsert_notices(self, rows):
        for i in range(0, len(rows), 200):
            chunk = [{k: v for k, v in r.items()} for r in rows[i:i + 200]]
            self._req("POST", "notices?on_conflict=ocid", chunk, {"Prefer": "resolution=merge-duplicates"})

    def open_notices(self):
        today = dt.date.today().isoformat()
        out, offset = [], 0
        while True:
            page = self._req("GET", f"notices?or=(deadline.is.null,deadline.gte.{today})&select=*&limit=1000&offset={offset}")
            out += page
            if len(page) < 1000:
                return out
            offset += 1000

    def set_summary(self, ocid, summary):
        self._req("PATCH", f"notices?ocid=eq.{urllib.parse.quote(ocid)}", {"summary": summary})

    def subscribers(self):
        return self._req("GET", "subscribers?status=eq.active&select=*")

    def all_subscribers(self):
        return self._req("GET", "subscribers?select=*")

    def pending_unconfirmed(self):
        return self._req("GET", "subscribers?status=eq.pending&confirm_sent_at=is.null&select=*")

    def delete_stale_pending(self, before_iso):
        self._req("DELETE", f"subscribers?status=eq.pending&created_at=lt.{urllib.parse.quote(before_iso)}")

    def update_subscriber(self, sid, fields):
        self._req("PATCH", f"subscribers?id=eq.{sid}", fields)

    def insert_subscriber(self, row):
        self._req("POST", "subscribers", [row])

    def sent_ocids(self, sid):
        return {r["ocid"] for r in self._req("GET", f"sent?subscriber_id=eq.{sid}&select=ocid")}

    def mark_sent(self, sid, ocids):
        if ocids:
            self._req("POST", "sent", [{"subscriber_id": sid, "ocid": o, "sent_at": now()} for o in ocids],
                      {"Prefer": "resolution=ignore-duplicates"})


def store():
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        return SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    return SQLiteStore(os.environ.get("DB_PATH", "bidcanary.db"))


# ---------------------------------------------------------------- fetch

def parse_release(rel: dict) -> dict | None:
    """Flatten one OCDS release. Returns None unless it advertises an opportunity."""
    tags = rel.get("tag", [])
    if not any(t in tags for t in ("tender", "tenderUpdate")):
        return None
    t = rel.get("tender", {})
    cpvs = set()
    classes = [t.get("classification") or {}]
    for it in t.get("items", []):
        classes += [it.get("classification") or {}] + it.get("additionalClassifications", [])
    for c in classes:
        if c.get("scheme", "").upper() == "CPV" and c.get("id"):
            cpvs.add(str(c["id"]))
    regions = {a["region"] for it in t.get("items", []) for a in it.get("deliveryAddresses", []) if a.get("region")}
    buyer = (rel.get("buyer") or {}).get("name") or next(
        (p.get("name") for p in rel.get("parties", []) if "buyer" in p.get("roles", [])), "")
    url = next((d.get("url") for d in t.get("documents", []) if "find-tender" in (d.get("url") or "")), None)
    return {
        "ocid": rel.get("ocid"), "title": t.get("title", ""), "description": (t.get("description") or "")[:4000],
        "buyer": buyer, "value": (t.get("value") or {}).get("amount"),
        "deadline": (t.get("tenderPeriod") or {}).get("endDate"),
        "cpvs": sorted(cpvs), "regions": sorted(regions),
        "url": url or f"https://www.find-tender.service.gov.uk/Procurement/{rel.get('ocid')}",
        "seen_at": now(),
    }


def fetch(s) -> int:
    start = s.get_state("last_fetch") or (dt.datetime.utcnow() - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    end = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    url = f"{FTS_URL}?{urllib.parse.urlencode({'updatedFrom': start, 'updatedTo': end, 'limit': 100})}"
    rows = []
    while url:
        pkg = http_json(url)
        rows += [r for r in (parse_release(x) for x in pkg.get("releases", [])) if r and r["ocid"]]
        url = (pkg.get("links") or {}).get("next")
    latest = {}
    for r in rows:  # the feed can repeat a notice (original + updates); keep the newest
        latest[r["ocid"]] = r
    s.upsert_notices(list(latest.values()))
    s.set_state("last_fetch", end)
    return len(rows)


# ---------------------------------------------------------------- match

def topical(n: dict, pack: str) -> bool:
    p = PACKS[pack]
    text = f"{n['title']} {n['description']}".lower()
    return any(c.startswith(x) for c in n["cpvs"] for x in p["cpv"]) or any(k in text for k in p["keywords"])


def in_regions(n: dict, wanted: list[str]) -> bool:
    # Notices with no stated location are kept: buyers often omit it.
    return not wanted or not n["regions"] or any(r.upper().startswith(w) for r in n["regions"] for w in wanted)


def matches(n: dict, sub: dict) -> bool:
    wanted = [r.strip().upper() for r in (sub.get("regions") or "").split(",") if r.strip()]
    return topical(n, sub["pack"]) and in_regions(n, wanted)


def build_counts(s, path="site/data/counts.json") -> dict:
    """Public, aggregate-only data for the site's 'check your area' tool."""
    notices = s.open_notices()
    out = {"updated": now(), "packs": {}}
    for pack, meta in PACKS.items():
        per = {}
        for code in REGIONS:
            hits = [n for n in notices if topical(n, pack) and in_regions(n, [code])]
            hits.sort(key=lambda n: n["deadline"] or "9999")
            per[code] = {"count": len(hits), "examples": [
                {"title": n["title"][:120], "buyer": n["buyer"][:80], "value": n["value"],
                 "deadline": (n["deadline"] or "")[:10], "url": n["url"]} for n in hits[:3]]}
        out["packs"][pack] = {"label": meta["label"], "regions": per}
    out["regionNames"] = REGIONS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f)
    return out


# ---------------------------------------------------------------- summarise

def summarise(n: dict) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return fallback_summary(n)
    prompt = ("You write for the owner of a small UK cleaning, security or grounds-maintenance firm. "
              "In at most 3 short sentences of plain English, say what the buyer wants, the scale, and any "
              "requirement that would rule a small firm out. No hype. Use only facts in the notice.\n\n"
              f"Title: {n['title']}\nBuyer: {n['buyer']}\nValue: {n['value']}\nDeadline: {n['deadline']}\n"
              f"Description: {n['description']}")
    body = json.dumps({"model": os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5"), "max_tokens": 200,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        r = http_json("https://api.anthropic.com/v1/messages", body,
                      {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        return r["content"][0]["text"].strip()
    except Exception:
        return fallback_summary(n)


def fallback_summary(n: dict) -> str:
    d = " ".join((n.get("description") or "").split())
    return (d[:280] + "…") if len(d) > 280 else d


# ---------------------------------------------------------------- stripe

def sync_stripe(s) -> dict:
    """Set each subscriber's plan from active Stripe subscriptions (matched by email)."""
    key = os.environ["STRIPE_SECRET_KEY"]
    prices = {os.environ.get("STRIPE_PRICE_STARTER"): "starter", os.environ.get("STRIPE_PRICE_PRO"): "pro"}
    paid, url = {}, "https://api.stripe.com/v1/subscriptions?status=active&limit=100&expand[]=data.customer"
    while url:
        page = http_json(url, headers={"Authorization": f"Bearer {key}"})
        for sub in page["data"]:
            email = ((sub.get("customer") or {}).get("email") or "").lower()
            for item in sub["items"]["data"]:
                plan = prices.get(item["price"]["id"])
                if email and plan and (paid.get(email) != "pro"):
                    paid[email] = plan
        url = (f"https://api.stripe.com/v1/subscriptions?status=active&limit=100&expand[]=data.customer"
               f"&starting_after={page['data'][-1]['id']}") if page.get("has_more") else None
    return apply_plans(s, paid)


def apply_plans(s, paid: dict) -> dict:
    report = {"upgraded": [], "downgraded": [], "paid_without_profile": []}
    known = {x["email"].lower(): x for x in s.all_subscribers()}
    for email, plan in paid.items():
        sub = known.get(email)
        if not sub:
            report["paid_without_profile"].append(email)  # escalate: ask them to set an alert profile
        elif sub["plan"] != plan:
            s.update_subscriber(sub["id"], {"plan": plan, "status": "active"}); report["upgraded"].append(email)
    for email, sub in known.items():
        if sub["plan"] in ("starter", "pro") and email not in paid:
            s.update_subscriber(sub["id"], {"plan": "free"}); report["downgraded"].append(email)
    return report


# ---------------------------------------------------------------- double opt-in

def render_confirmation(sub: dict) -> str:
    link = f"{SITE_URL}/confirm.html?t={sub['unsub_token']}"
    address = html.escape(os.environ.get("POSTAL_ADDRESS", ""))
    return f"""<div style="max-width:560px;margin:auto;padding:24px;font:15px/1.6 system-ui;color:#0e1512">
    <h1 style="font:700 20px system-ui">Confirm your Bid Canary alerts</h1>
    <p>Someone (hopefully you) asked for weekly public sector tender alerts for
    {html.escape(PACKS.get(sub['pack'], {'label': sub['pack']})['label'].lower())} to be sent to this address.</p>
    <p><a href="{link}" style="display:inline-block;background:#FFD43B;color:#0e1512;font-weight:600;padding:12px 20px;border-radius:999px;text-decoration:none">Yes, send me tenders</a></p>
    <p style="color:#475569">If this wasn't you, ignore this email and you won't hear from us again. Unconfirmed sign-ups are deleted after 7 days.</p>
    <p style="font-size:12px;color:#64748b">Bid Canary{(' · ' + address) if address else ''}</p></div>"""


def send_confirmations(s, dry_run=False) -> dict:
    """Email a confirmation link to new sign-ups; delete ones never confirmed after 7 days."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).replace(microsecond=0).isoformat()
    s.delete_stale_pending(cutoff)
    sent = []
    for sub in s.pending_unconfirmed():
        if not dry_run:
            send_email(sub["email"], "Confirm your Bid Canary tender alerts", render_confirmation(sub),
                       f"{SITE_URL}/unsubscribe.html?t={sub['unsub_token']}")
        s.update_subscriber(sub["id"], {"confirm_sent_at": now()})
        sent.append(sub["email"])
    return {"confirmation_emails": len(sent)}


# ---------------------------------------------------------------- digest

def money(v) -> str:
    return "Value not stated" if v in (None, "") else f"£{float(v):,.0f}"


def render_digest(items: list[dict], sub: dict, extra_count: int = 0) -> str:
    paid = sub["plan"] in ("starter", "pro")
    rows = "".join(
        f"""<tr><td style="padding:16px 0;border-bottom:1px solid #e5e7eb">
        <a href="{html.escape(i['url'])}" style="font:600 16px system-ui;color:#0e1512">{html.escape(i['title'])}</a>
        <div style="font:13px system-ui;color:#475569;margin:4px 0">{html.escape(i['buyer'] or '')} · {money(i['value'])}
        · Closes {html.escape((i['deadline'] or 'date not stated')[:10])}</div>
        {f'<div style="font:14px/1.5 system-ui;color:#1e293b">{html.escape(i.get("summary") or "")}</div>' if paid else ''}
        </td></tr>""" for i in items)
    upsell = "" if paid else (
        f"""<p style="font:14px/1.5 system-ui;background:#fff6d6;padding:14px;border-radius:10px">
        {'<b>' + str(extra_count) + ' more matching tenders</b> this week. ' if extra_count else ''}
        Get every match the morning it's published, with plain-English summaries, for £29 a month.
        <a href="{SITE_URL}/#pricing">Upgrade</a></p>""")
    unsub = f"{SITE_URL}/unsubscribe.html?t={sub['unsub_token']}"
    address = html.escape(os.environ.get("POSTAL_ADDRESS", ""))
    head = "today" if paid else "this week"
    return f"""<div style="max-width:600px;margin:auto;padding:24px">
    <h1 style="font:700 20px system-ui;color:#0e1512">{len(items) + extra_count} new tender{'s' if len(items) + extra_count != 1 else ''} for you {head}</h1>
    {upsell}<table width="100%" cellspacing="0">{rows}</table>
    <p style="font:12px system-ui;color:#64748b;margin-top:24px">{ATTRIBUTION}<br>
    Summaries are a guide; always read the full notice before bidding.<br>
    Bid Canary{(' · ' + address) if address else ''} · <a href="{unsub}">Unsubscribe</a></p></div>"""


def send_email(to: str, subject: str, html_body: str, unsub_url: str) -> None:
    body = json.dumps({"from": os.environ["FROM_EMAIL"], "to": [to], "subject": subject, "html": html_body,
                       "reply_to": os.environ.get("REPLY_TO", "hello@bidcanary.co.uk"),
                       "headers": {"List-Unsubscribe": f"<{unsub_url}>"}}).encode()
    http_json("https://api.resend.com/emails", body,
              {"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}", "Content-Type": "application/json"})


def digest(s, dry_run=False, today: dt.date | None = None, free_limit=5) -> dict:
    today = today or dt.date.today()
    weekly = today.weekday() == 0  # Monday
    notices = s.open_notices()
    report = {}
    for sub in s.subscribers():
        paid = sub["plan"] in ("starter", "pro")
        if not paid and not weekly:
            continue
        seen = s.sent_ocids(sub["id"])
        picked = sorted((n for n in notices if n["ocid"] not in seen and matches(n, sub)),
                        key=lambda n: n["deadline"] or "9999")
        if not picked:
            continue
        shown, extra = (picked, 0) if paid else (picked[:free_limit], len(picked) - free_limit if len(picked) > free_limit else 0)
        if paid:
            for n in shown:
                if not n.get("summary"):
                    n["summary"] = summarise(n); s.set_summary(n["ocid"], n["summary"])
        page = render_digest(shown, sub, extra)
        subject = f"{len(picked)} new {PACKS[sub['pack']]['label'].lower()} tender{'s' if len(picked) != 1 else ''}"
        if not dry_run:
            send_email(sub["email"], f"Bid Canary: {subject}", page, f"{SITE_URL}/unsubscribe.html?t={sub['unsub_token']}")
        s.mark_sent(sub["id"], [n["ocid"] for n in picked])
        report[sub["email"]] = {"plan": sub["plan"], "count": len(picked), "html": page}
    return report


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    s = store()
    if cmd == "fetch":
        n = fetch(s); build_counts(s); print(f"stored {n} releases; counts rebuilt")
    elif cmd == "confirmations":
        print(json.dumps(send_confirmations(s, dry_run="--dry-run" in sys.argv)))
    elif cmd == "sync-stripe":
        print(json.dumps(sync_stripe(s)))
    elif cmd == "digest":
        r = digest(s, dry_run="--dry-run" in sys.argv)
        print(json.dumps({k: {"plan": v["plan"], "count": v["count"]} for k, v in r.items()}))
    else:
        print(__doc__)
