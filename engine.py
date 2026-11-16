"""Bid Canary alert engine (v0.2, free-tier stack).

Pulls new notices from the free Find a Tender OCDS API (Open Government Licence v3.0),
matches them to subscribers, and emails digests:
  * trial:        every new sign-up gets the daily Starter digest free for 14 days after confirming
  * free plan:    weekly email (Mondays), one showcase tender with its summary, plus a count of the rest
  * starter/pro:  daily digest with plain-English summaries (monthly or annual)
It also collects suppliers named on contract award notices, for outreach.py (cold outreach).

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
    STRIPE_SECRET_KEY (restricted, read-only), STRIPE_PRICE_STARTER, STRIPE_PRICE_STARTER_ANNUAL, STRIPE_PRICE_PRO
    OWNER_EMAIL                          where owner reports go (default support@bidcanary.co.uk)
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
PRICE_MONTHLY, PRICE_ANNUAL = "£29.99", "£299"
FOUNDING_LIMIT = 50  # first N paying customers keep today's price for as long as they stay subscribed
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "support@bidcanary.co.uk")
ATTRIBUTION = ("Contains public sector information licensed under the Open Government "
               "Licence v3.0. Source: Find a Tender, Cabinet Office.")
SITE_URL = os.environ.get("SITE_URL", "https://bidcanary.co.uk").rstrip("/")

# CPV prefixes match the code and all its children.
PACKS = {
    # Marketing leads with cleaning; the product covers every main public sector category.
    "cleaning": {"label": "Cleaning", "cpv": ["909", "3983"], "keywords": ["cleaning", "janitorial", "washroom", "hygiene services"]},
    "security": {"label": "Security", "cpv": ["7971", "5161"], "keywords": ["security services", "guarding", "cctv monitoring", "key holding", "alarm response"]},
    "grounds": {"label": "Grounds maintenance", "cpv": ["7731", "7721"], "keywords": ["grounds maintenance", "landscaping", "grass cutting", "arboricultur"]},
    "fm": {"label": "Facilities management", "cpv": ["7952", "5070"], "keywords": ["facilities management", "soft fm", "hard fm", "caretaking"]},
    "construction": {"label": "Construction and building works", "cpv": ["45"], "keywords": ["construction works", "refurbishment", "building works", "roofing", "groundworks"]},
    "engineering": {"label": "Architecture, engineering and surveying", "cpv": ["71"], "keywords": ["architect", "surveying", "structural engineer", "quantity surveyor"]},
    "repairs": {"label": "Building maintenance and trades", "cpv": ["5070", "5071", "5072", "5073", "4533", "4542", "4544", "4545"], "keywords": ["repairs and maintenance", "electrical maintenance", "plumbing", "painting and decorating", "gas servicing"]},
    "it": {"label": "IT, software and digital", "cpv": ["72", "48", "302", "324"], "keywords": ["software", "digital services", "it support", "cyber security", "website"]},
    "health": {"label": "Health and social care", "cpv": ["85"], "keywords": ["care services", "domiciliary care", "social care", "healthcare services"]},
    "medical": {"label": "Medical equipment and supplies", "cpv": ["33"], "keywords": ["medical equipment", "medical supplies", "pharmaceutical"]},
    "education": {"label": "Education and training", "cpv": ["80"], "keywords": ["training services", "training provider", "education services", "apprenticeship"]},
    "transport": {"label": "Transport, fleet and vehicles", "cpv": ["60", "34", "501"], "keywords": ["transport services", "fleet", "vehicle", "passenger transport", "taxi"]},
    "catering": {"label": "Catering and food", "cpv": ["555", "15"], "keywords": ["catering", "food supply", "school meals", "vending"]},
    "professional": {"label": "Consultancy and professional services", "cpv": ["791", "792", "794", "73"], "keywords": ["consultancy", "legal services", "accountancy", "audit", "research services"]},
    "marketing": {"label": "Marketing, design and communications", "cpv": ["7934", "7982", "7996", "7941"], "keywords": ["marketing", "communications", "graphic design", "printing", "video production"]},
    "staffing": {"label": "Recruitment and staffing", "cpv": ["7961", "7962"], "keywords": ["recruitment", "temporary staff", "agency workers", "staffing"]},
    "waste": {"label": "Waste and environmental services", "cpv": ["905", "906", "907", "908"], "keywords": ["waste collection", "recycling", "environmental services", "pest control"]},
    "energy": {"label": "Energy and utilities", "cpv": ["09", "65", "4533"], "keywords": ["energy", "electricity supply", "solar", "heat pump", "ev charging"]},
    "supplies": {"label": "Furniture, equipment and supplies", "cpv": ["39", "30", "22", "18"], "keywords": ["furniture", "office supplies", "stationery", "uniforms", "workwear"]},
    "custom": {"label": "Something else (use my keywords)", "cpv": [], "keywords": []},
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
    CREATE TABLE IF NOT EXISTS subscribers (id TEXT PRIMARY KEY, email TEXT UNIQUE, pack TEXT, regions TEXT, keywords TEXT,
      plan TEXT DEFAULT 'free', status TEXT DEFAULT 'pending', unsub_token TEXT, created_at TEXT, confirm_sent_at TEXT,
      trial_ends_at TEXT, trial_end_notified_at TEXT, founding INTEGER DEFAULT 0,
      ref_code TEXT UNIQUE DEFAULT (lower(hex(randomblob(4)))), referred_by TEXT, referral_flagged_at TEXT);
    CREATE TABLE IF NOT EXISTS outreach_contacts (id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
      company_number TEXT UNIQUE, company_name TEXT, email TEXT, contact_name TEXT, region TEXT, pack TEXT,
      source_ocid TEXT, award_title TEXT, company_type TEXT, status TEXT DEFAULT 'new', skip_reason TEXT, subject TEXT,
      emailed_at TEXT, followed_up_at TEXT, responded_at TEXT,
      optout_token TEXT DEFAULT (lower(hex(randomblob(16)))), created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS outreach_suppression (key TEXT PRIMARY KEY, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
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

    def outreach_add(self, rows):
        for r in rows:
            cols = ",".join(r); q = ",".join("?" * len(r))
            self.con.execute(f"INSERT OR IGNORE INTO outreach_contacts ({cols}) VALUES ({q})", tuple(r.values()))
        self.con.commit()

    def outreach_rows(self, statuses=None):
        if statuses is None:
            return [dict(r) for r in self.con.execute("SELECT * FROM outreach_contacts")]
        q = ",".join("?" * len(statuses))
        return [dict(r) for r in self.con.execute(f"SELECT * FROM outreach_contacts WHERE status IN ({q})", statuses)]

    def outreach_update(self, cid, fields):
        sets = ",".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE outreach_contacts SET {sets} WHERE id=?", (*fields.values(), cid)); self.con.commit()

    def outreach_delete_older_than(self, iso):  # local stand-in for the database's prune_outreach()
        self.con.execute("DELETE FROM outreach_contacts WHERE status IN ('new','checked','skipped','emailed','followed_up') "
                         "AND created_at < ?", (iso,)); self.con.commit()

    def suppression_keys(self):
        return {r["key"] for r in self.con.execute("SELECT key FROM outreach_suppression")}

    def suppress(self, keys, reason):
        self.con.executemany("INSERT OR IGNORE INTO outreach_suppression (key, reason) VALUES (?,?)", [(k, reason) for k in keys])
        self.con.commit()


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

    def outreach_add(self, rows):
        for i in range(0, len(rows), 200):
            self._req("POST", "outreach_contacts?on_conflict=company_number", rows[i:i + 200],
                      {"Prefer": "resolution=ignore-duplicates,return=minimal"})

    def outreach_rows(self, statuses=None):
        flt = f"status=in.({','.join(statuses)})&" if statuses is not None else ""
        out, offset = [], 0
        while True:
            page = self._req("GET", f"outreach_contacts?{flt}select=*&order=created_at.desc&limit=1000&offset={offset}")
            out += page
            if len(page) < 1000:
                return out
            offset += 1000

    def outreach_update(self, cid, fields):
        self._req("PATCH", f"outreach_contacts?id=eq.{cid}", fields)

    def suppression_keys(self):
        out, offset = set(), 0
        while True:
            page = self._req("GET", f"outreach_suppression?select=key&limit=1000&offset={offset}")
            out |= {r["key"] for r in page}
            if len(page) < 1000:
                return out
            offset += 1000

    def suppress(self, keys, reason):
        if keys:
            self._req("POST", "outreach_suppression?on_conflict=key", [{"key": k, "reason": reason} for k in keys],
                      {"Prefer": "resolution=ignore-duplicates,return=minimal"})


def store():
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        return SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    return SQLiteStore(os.environ.get("DB_PATH", "bidcanary.db"))


# ---------------------------------------------------------------- fetch

NATIONAL = ["UK"]


def resolve_regions(delivery: list, buyer_region: str | None) -> list[str]:
    """Where the work is. Specific delivery regions first; else the buyer's region.
    Returns ["UK"] for UK-wide work (delivery says "UK" or spans 6+ regions with no better clue)."""
    codes = sorted({str(c).strip().upper() for c in delivery if c and str(c).strip().upper().startswith("UK")})
    specific = [c for c in codes if len(c) >= 3 and c[:3] in REGIONS]
    level1 = {c[:3] for c in specific}
    if specific and len(level1) < 6:
        return specific
    b = str(buyer_region or "").strip().upper()
    if b[:3] in REGIONS and not (specific and len(level1) >= 6):
        return [b]
    return NATIONAL


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
    buyer_party = next((p for p in rel.get("parties", []) if "buyer" in p.get("roles", [])), {})
    buyer = (rel.get("buyer") or {}).get("name") or buyer_party.get("name", "")
    regions = resolve_regions(
        [a.get("region") for it in t.get("items", []) for a in it.get("deliveryAddresses", [])],
        (buyer_party.get("address") or {}).get("region"))
    url = next((d.get("url") for d in t.get("documents", []) if "find-tender" in (d.get("url") or "")), None)
    return {
        "ocid": rel.get("ocid"), "title": t.get("title", ""), "description": (t.get("description") or "")[:4000],
        "buyer": buyer, "value": (t.get("value") or {}).get("amount"),
        "deadline": (t.get("tenderPeriod") or {}).get("endDate"),
        "cpvs": sorted(cpvs), "regions": sorted(regions),
        "url": url or f"https://www.find-tender.service.gov.uk/Procurement/{rel.get('ocid')}",
        "seen_at": now(),
    }


def company_number(party: dict) -> str | None:
    """Companies House number from an OCDS party (identifier or additionalIdentifiers, scheme GB-COH)."""
    for ident in [party.get("identifier") or {}] + (party.get("additionalIdentifiers") or []):
        if str(ident.get("scheme", "")).upper() == "GB-COH" and ident.get("id"):
            num = str(ident["id"]).replace(" ", "").upper()
            num = num.zfill(8) if num.isdigit() else num
            if len(num) == 8 and num.isalnum():
                return num
    return None


def pack_for(title: str, description: str, cpvs: list[str]) -> str | None:
    """Best sector for an award, preferring the sectors marketing leads with."""
    n = {"title": title or "", "description": description or "", "cpvs": cpvs}
    order = ["cleaning", "security", "grounds", "fm"] + [p for p in PACKS if p not in ("cleaning", "security", "grounds", "fm", "custom")]
    return next((p for p in order if topical(n, p)), None)


def parse_suppliers(rel: dict) -> list[dict]:
    """Suppliers named on a contract award, with a Companies House number and a contact email.
    These are public award-notice details, used only for outreach.py and its safeguards."""
    tags = rel.get("tag", [])
    if not any(t in tags for t in ("award", "awardUpdate", "contract", "contractUpdate")):
        return []
    t = rel.get("tender") or {}
    awards = rel.get("awards") or []
    title = (awards[0].get("title") if awards else None) or t.get("title") or ""
    cpvs = set()
    for c in [t.get("classification") or {}] + [it.get("classification") or {} for it in t.get("items", [])]:
        if c.get("scheme", "").upper() == "CPV" and c.get("id"):
            cpvs.add(str(c["id"]))
    pack = pack_for(title, t.get("description") or "", sorted(cpvs))
    out = []
    for p in rel.get("parties", []):
        if "supplier" not in p.get("roles", []):
            continue
        num, email = company_number(p), ((p.get("contactPoint") or {}).get("email") or "").strip().lower()
        if not num or "@" not in email or len(email) > 254:
            continue
        region = str((p.get("address") or {}).get("region") or "").upper()[:3]
        out.append({"company_number": num, "company_name": (p.get("name") or "")[:200], "email": email,
                    "contact_name": ((p.get("contactPoint") or {}).get("name") or "")[:120] or None,
                    "region": region if region in REGIONS else None, "pack": pack,
                    "source_ocid": rel.get("ocid"), "award_title": title[:300]})
    return out


def fetch(s, days_back: int | None = None) -> int:
    start = (None if days_back else s.get_state("last_fetch")) or (
        dt.datetime.utcnow() - dt.timedelta(days=days_back or 3)).strftime("%Y-%m-%dT%H:%M:%S")
    end = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    url = f"{FTS_URL}?{urllib.parse.urlencode({'updatedFrom': start, 'updatedTo': end, 'limit': 100})}"
    rows, suppliers = [], {}
    while url:
        pkg = http_json(url)
        rows += [r for r in (parse_release(x) for x in pkg.get("releases", [])) if r and r["ocid"]]
        for x in pkg.get("releases", []):
            for sup in parse_suppliers(x):
                suppliers.setdefault(sup["company_number"], sup)
        url = (pkg.get("links") or {}).get("next")
    latest = {}
    for r in rows:  # the feed can repeat a notice (original + updates); keep the newest
        latest[r["ocid"]] = r
    s.upsert_notices(list(latest.values()))
    if suppliers and hasattr(s, "outreach_add"):
        try:
            s.outreach_add(list(suppliers.values()))
        except RuntimeError as e:  # outreach table not created yet (migration 004): alerts must keep working
            print("outreach contacts not saved:", e)
    s.set_state("last_fetch", end)
    return len(rows)


# ---------------------------------------------------------------- match

def sector_phrase(sub: dict) -> str:
    """Words for emails: 'cleaning tenders', or 'tenders matching your keywords' for custom profiles."""
    if sub.get("pack") == "custom" or sub.get("pack") not in PACKS:
        return "tenders matching your keywords"
    label = PACKS[sub["pack"]]["label"]
    if not label[:2].isupper():  # keep acronyms like "IT"
        label = label[0].lower() + label[1:]
    return label + " tenders"


def keyword_list(raw: str | None) -> list[str]:
    return [k.strip().lower() for k in (raw or "").split(",") if len(k.strip()) >= 3][:10]


def topical(n: dict, pack: str, extra_keywords: list[str] | None = None) -> bool:
    """Sector match by CPV code or keyword, plus any customer keywords (which widen the match)."""
    p = PACKS.get(pack, PACKS["custom"])
    text = f"{n['title']} {n['description']}".lower()
    words = p["keywords"] + (extra_keywords or [])
    return any(c.startswith(x) for c in n["cpvs"] for x in p["cpv"]) or any(k in text for k in words)


def is_national(n: dict) -> bool:
    return not n["regions"] or n["regions"] == NATIONAL


def in_regions(n: dict, wanted: list[str]) -> bool:
    """No regions chosen = everything. Otherwise only work located in those regions (UK-wide work excluded)."""
    if not wanted:
        return True
    return not is_national(n) and any(r.upper().startswith(w) for r in n["regions"] for w in wanted)


def matches(n: dict, sub: dict) -> bool:
    wanted = [r.strip().upper() for r in (sub.get("regions") or "").split(",") if r.strip()]
    return topical(n, sub["pack"], keyword_list(sub.get("keywords"))) and in_regions(n, wanted)


def build_counts(s, path="site/data/counts.json") -> dict:
    """Public, aggregate-only data for the site's 'check your area' tool."""
    notices = s.open_notices()
    out = {"updated": now(), "packs": {}}
    for pack, meta in PACKS.items():
        if pack == "custom":
            continue
        per = {}
        national = [n for n in notices if topical(n, pack) and is_national(n)]
        for code in REGIONS:
            hits = [n for n in notices if topical(n, pack) and in_regions(n, [code])]
            hits.sort(key=lambda n: n["deadline"] or "9999")
            per[code] = {"count": len(hits), "examples": [
                {"title": n["title"][:120], "buyer": n["buyer"][:80], "value": n["value"],
                 "deadline": (n["deadline"] or "")[:10], "url": n["url"]} for n in hits[:3]]}
        out["packs"][pack] = {"label": meta["label"], "regions": per, "national": len(national)}
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
    prices = {os.environ.get("STRIPE_PRICE_STARTER"): "starter", os.environ.get("STRIPE_PRICE_STARTER_ANNUAL"): "starter",
              os.environ.get("STRIPE_PRICE_PRO"): "pro"}
    prices.pop(None, None); prices.pop("", None)
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
    """Sync plans, record founding members, and flag referral rewards for the owners to apply in Stripe."""
    report = {"upgraded": [], "downgraded": [], "paid_without_profile": [], "new_founding_members": [],
              "referral_rewards_due": []}
    subs = s.all_subscribers()
    known = {x["email"].lower(): x for x in subs}
    by_code = {x.get("ref_code"): x for x in subs if x.get("ref_code")}
    founding = sum(1 for x in subs if x.get("founding"))
    for email, plan in paid.items():
        sub = known.get(email)
        if not sub:
            report["paid_without_profile"].append(email)  # escalate: ask them to set an alert profile
            continue
        fields = {}
        if sub["plan"] != plan:
            fields.update(plan=plan, status="active"); report["upgraded"].append(email)
        if not sub.get("founding") and founding < FOUNDING_LIMIT:
            fields["founding"] = True; founding += 1; report["new_founding_members"].append(email)
        ref = by_code.get(sub.get("referred_by"))
        if ref and not sub.get("referral_flagged_at") and ref["email"].lower() != email:
            fields["referral_flagged_at"] = now()
            report["referral_rewards_due"].append({"referrer": ref["email"], "new_customer": email})
        if fields:
            s.update_subscriber(sub["id"], fields)
    for email, sub in known.items():
        if sub["plan"] in ("starter", "pro") and email not in paid:
            s.update_subscriber(sub["id"], {"plan": "free"}); report["downgraded"].append(email)
    report["founding_members_total"] = founding
    if founding >= FOUNDING_LIMIT:
        report["notice"] = f"Founding offer is full ({FOUNDING_LIMIT}). Remove it from the website and terms."
    return report


def mask(email: str) -> str:
    """Logs from a public repo are public: never print a whole address."""
    return "***@" + email.split("@")[-1] if "@" in (email or "") else "***"


def log_safe(obj):
    """Copy of a report with every email address masked."""
    if isinstance(obj, dict):
        return {(mask(k) if "@" in str(k) else k): log_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [log_safe(v) for v in obj]
    return mask(obj) if isinstance(obj, str) and "@" in obj and " " not in obj else obj


def owner_report(report: dict) -> str | None:
    """Plain-text note for the owners when there is something to act on, else None."""
    lines = []
    for r in report.get("referral_rewards_due", []):
        lines.append(f"Referral reward due: give one free month to {r['referrer']} and to {r['new_customer']} "
                     "(Stripe > Customers > Apply coupon).")
    for e in report.get("new_founding_members", []):
        lines.append(f"New founding member (price locked at {PRICE_MONTHLY}/month or {PRICE_ANNUAL}/year): {e}")
    for e in report.get("paid_without_profile", []):
        lines.append(f"Paid in Stripe but no alert profile: {e}. Ask them to sign up on the website with the same email.")
    if report.get("notice"):
        lines.append(report["notice"])
    return "\n".join(lines) or None


def notify_owners(subject: str, text: str) -> None:
    """Short internal note to the owners (transactional, to our own address)."""
    if os.environ.get("RESEND_API_KEY") and os.environ.get("FROM_EMAIL"):
        send_email(OWNER_EMAIL, subject, "<pre style='font:14px/1.5 system-ui;white-space:pre-wrap'>" + html.escape(text) + "</pre>",
                   f"mailto:{OWNER_EMAIL}")
    print(subject + " (details emailed to the owners; not printed because Actions logs are public)")


# ---------------------------------------------------------------- trial

TRIAL_DAYS = 14


def _ts(v) -> dt.datetime | None:
    if not v:
        return None
    t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def on_trial(sub: dict, at: dt.datetime | None = None) -> bool:
    end = _ts(sub.get("trial_ends_at"))
    return bool(end) and (at or dt.datetime.now(dt.timezone.utc)) < end


def gets_daily(sub: dict, at: dt.datetime | None = None) -> bool:
    """Paid plans, and anyone still in their free trial, get the full daily digest."""
    return sub["plan"] in ("starter", "pro") or on_trial(sub, at)


# ---------------------------------------------------------------- double opt-in

def render_confirmation(sub: dict) -> str:
    link = f"{SITE_URL}/confirm.html?t={sub['unsub_token']}"
    address = html.escape(os.environ.get("POSTAL_ADDRESS", ""))
    return f"""<div style="max-width:560px;margin:auto;padding:24px;font:15px/1.6 system-ui;color:#0e1512">
    <h1 style="font:700 20px system-ui">Confirm your Bid Canary alerts</h1>
    <p>Someone (hopefully you) asked for public sector tender alerts
    ({html.escape(sector_phrase(sub))}) to be sent to this address.</p>
    <p>Once you confirm, you get {TRIAL_DAYS} days of daily alerts free, with a plain-English summary of every match.
    After that you'll get our free weekly email unless you choose to upgrade. No card needed.</p>
    <p><a href="{link}" style="display:inline-block;background:#FFD43B;color:#0e1512;font-weight:600;padding:12px 20px;border-radius:999px;text-decoration:none">Yes, send me tenders</a></p>
    <p style="color:#475569">If this wasn't you, ignore this email and you won't hear from us again. Unconfirmed sign-ups are deleted after 7 days.</p>
    <p style="color:#475569">Bid Canary sends public sector tender alerts to businesses. Questions? Email
    <a href="mailto:support@bidcanary.co.uk">support@bidcanary.co.uk</a>.</p>
    <p style="font-size:12px;color:#64748b">Bid Canary · <a href="{SITE_URL}">bidcanary.co.uk</a>{(' · ' + address) if address else ''}</p></div>"""


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


def referral_line(sub: dict) -> str:
    if not sub.get("ref_code"):
        return ""
    link = f"{SITE_URL}/?ref={sub['ref_code']}"
    return (f'<p style="font:13px/1.5 system-ui;color:#475569">Know another business that bids for public contracts? '
            f'Send them <a href="{link}">your link</a>. When they become a paying customer, you both get a month free.</p>')


def render_digest(items: list[dict], sub: dict, extra_count: int = 0, at: dt.datetime | None = None) -> str:
    paid = gets_daily(sub, at)
    trial_note = ""
    if on_trial(sub, at) and sub["plan"] not in ("starter", "pro"):
        end = _ts(sub["trial_ends_at"]); left = max((end - (at or dt.datetime.now(dt.timezone.utc))).days, 0)
        trial_note = (f"""<p style="font:14px/1.5 system-ui;background:#fff6d6;padding:14px;border-radius:10px">
        <b>Free trial: {left} day{'s' if left != 1 else ''} left</b> (ends {end:%d %B}). After that you'll get one tender a week.
        Keep every match, every morning, for {PRICE_MONTHLY} a month or {PRICE_ANNUAL} a year.
        <a href="{SITE_URL}/#pricing">Keep daily alerts</a></p>""")
    rows = "".join(
        f"""<tr><td style="padding:16px 0;border-bottom:1px solid #e5e7eb">
        <a href="{html.escape(i['url'])}" style="font:600 16px system-ui;color:#0e1512">{html.escape(i['title'])}</a>
        <div style="font:13px system-ui;color:#475569;margin:4px 0">{html.escape(i['buyer'] or '')} · {money(i['value'])}
        · Closes {html.escape((i['deadline'] or 'date not stated')[:10])}</div>
        {f'<div style="font:14px/1.5 system-ui;color:#1e293b">{html.escape(i.get("summary") or "")}</div>' if i.get("summary") else ''}
        </td></tr>""" for i in items)
    upsell = "" if paid else (
        f"""<p style="font:14px/1.5 system-ui;background:#fff6d6;padding:14px;border-radius:10px">
        {('<b>You have ' + str(extra_count) + ' more matching tender' + ('s' if extra_count != 1 else '') + ' this week</b> that we haven\'t shown you. ') if extra_count else 'This is your free tender of the week. '}
        Starter sends every match the morning it's published, each with a plain-English summary like the one below, for {PRICE_MONTHLY} a month or {PRICE_ANNUAL} a year.
        <a href="{SITE_URL}/#pricing">Upgrade</a></p>""")
    unsub = f"{SITE_URL}/unsubscribe.html?t={sub['unsub_token']}"
    address = html.escape(os.environ.get("POSTAL_ADDRESS", ""))
    head = "today" if paid else "this week"
    return f"""<div style="max-width:600px;margin:auto;padding:24px">
    <h1 style="font:700 20px system-ui;color:#0e1512">{len(items) + extra_count} new tender{'s' if len(items) + extra_count != 1 else ''} for you {head}</h1>
    {trial_note}{upsell}<table width="100%" cellspacing="0">{rows}</table>
    {referral_line(sub)}
    <p style="font:12px system-ui;color:#64748b;margin-top:24px">{ATTRIBUTION}<br>
    Summaries are a guide; always read the full notice before bidding.<br>
    Bid Canary{(' · ' + address) if address else ''} · <a href="{unsub}">Unsubscribe</a></p></div>"""


def html_to_text(h: str) -> str:
    """Plain-text alternative for every email (spam filters distrust HTML-only mail)."""
    import re
    t = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', lambda m: f"{m.group(2)} ({m.group(1)})", h, flags=re.S)
    t = re.sub(r"<(br|/p|/h1|/div|/tr)[^>]*>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    lines = [" ".join(l.split()) for l in t.splitlines()]
    out = "\n".join(l for l in lines if l)
    return out


def send_email(to: str, subject: str, html_body: str, unsub_url: str) -> None:
    body = json.dumps({"from": os.environ["FROM_EMAIL"], "to": [to], "subject": subject, "html": html_body,
                       "text": html_to_text(html_body),
                       "reply_to": os.environ.get("REPLY_TO", "support@bidcanary.co.uk"),
                       "headers": {"List-Unsubscribe": f"<{unsub_url}>, <mailto:support@bidcanary.co.uk?subject=unsubscribe>"}}).encode()
    http_json("https://api.resend.com/emails", body,
              {"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}", "Content-Type": "application/json"})


def render_trial_ended(sub: dict) -> str:
    address = html.escape(os.environ.get("POSTAL_ADDRESS", ""))
    unsub = f"{SITE_URL}/unsubscribe.html?t={sub['unsub_token']}"
    return f"""<div style="max-width:560px;margin:auto;padding:24px;font:15px/1.6 system-ui;color:#0e1512">
    <h1 style="font:700 20px system-ui">Your free trial has ended</h1>
    <p>Thanks for trying Bid Canary. From now on you'll get our free weekly email: one hand-picked
    {html.escape(sector_phrase(sub).replace('tenders', 'tender', 1))} every Monday, plus a count of the others.</p>
    <p>To keep getting every match the morning it's published, with a plain-English summary of each, upgrade for
    {PRICE_MONTHLY} a month or {PRICE_ANNUAL} a year. Cancel any time.</p>
    <p><a href="{SITE_URL}/#pricing" style="display:inline-block;background:#FFD43B;color:#0e1512;font-weight:600;padding:12px 20px;border-radius:999px;text-decoration:none">Keep daily alerts</a></p>
    <p style="font-size:12px;color:#64748b">Bid Canary{(' · ' + address) if address else ''} · <a href="{unsub}">Unsubscribe</a></p></div>"""


def digest(s, dry_run=False, today: dt.date | None = None, free_limit=1) -> dict:
    today = today or dt.date.today()
    at = dt.datetime.combine(today, dt.time(6, 0), dt.timezone.utc)
    weekly = today.weekday() == 0  # Monday
    notices = s.open_notices()
    report = {}
    for sub in s.subscribers():
        paid = gets_daily(sub, at)
        if sub.get("trial_ends_at") and not on_trial(sub, at) and not sub.get("trial_end_notified_at"):
            if not paid:  # trial over and not upgraded: one short note, once (already-paying customers get none)
                if not dry_run:
                    send_email(sub["email"], "Your Bid Canary trial has ended", render_trial_ended(sub),
                               f"{SITE_URL}/unsubscribe.html?t={sub['unsub_token']}")
                report.setdefault("_trial_ended", []).append(sub["email"])
            s.update_subscriber(sub["id"], {"trial_end_notified_at": now()})
        if not paid and not weekly:
            continue
        seen = s.sent_ocids(sub["id"])
        picked = sorted((n for n in notices if n["ocid"] not in seen and matches(n, sub)),
                        key=lambda n: n["deadline"] or "9999")
        if not picked:
            continue
        if paid:
            shown, extra = picked, 0
        else:
            # Free plan: one showcase tender a week (largest stated value with at least
            # 10 days to bid, else the first match), with its summary as a taster.
            cutoff = (today + dt.timedelta(days=10)).isoformat()
            roomy = [n for n in picked if (n["deadline"] or "9999") >= cutoff]
            best = max(roomy or picked, key=lambda n: float(n["value"] or 0))
            shown = [best] + [n for n in picked if n is not best][:max(free_limit - 1, 0)]
            extra = len(picked) - len(shown)
        for n in shown:
            if not n.get("summary"):
                n["summary"] = summarise(n); s.set_summary(n["ocid"], n["summary"])
        page = render_digest(shown, sub, extra, at)
        subject = f"{len(picked)} new {sector_phrase(sub)}" if len(picked) != 1 else f"1 new {sector_phrase(sub).replace('tenders', 'tender', 1)}"
        if not dry_run:
            send_email(sub["email"], f"Bid Canary: {subject}", page, f"{SITE_URL}/unsubscribe.html?t={sub['unsub_token']}")
        s.mark_sent(sub["id"], [n["ocid"] for n in picked])
        report[sub["email"]] = {"plan": "trial" if on_trial(sub, at) and sub["plan"] == "free" else sub["plan"],
                                "count": len(picked), "html": page}
    return report


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    s = store()
    if cmd == "fetch":
        days = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
        n = fetch(s, days); c = build_counts(s); print(f"stored {n} releases; counts rebuilt")
        for pk, v in c["packs"].items():
            print(pk, "UK-wide:", v["national"], "|", " ".join(f"{k}:{r['count']}" for k, r in v["regions"].items()))
    elif cmd == "confirmations":
        print(json.dumps(send_confirmations(s, dry_run="--dry-run" in sys.argv)))
    elif cmd == "sync-stripe":
        rep_ = sync_stripe(s); print(json.dumps(log_safe(rep_)))
        note = owner_report(rep_)
        if note:
            notify_owners("Bid Canary: action needed (payments)", note)
    elif cmd == "digest":
        r = digest(s, dry_run="--dry-run" in sys.argv)
        print(json.dumps(log_safe({k: (v if k.startswith("_") else {"plan": v["plan"], "count": v["count"]}) for k, v in r.items()})))
    else:
        print(__doc__)
