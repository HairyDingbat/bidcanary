"""Bid Canary cold outreach (runs from GitHub Actions on weekdays).

Who it emails: suppliers named on UK public contract award notices (collected by `engine.py fetch`),
only where Companies House confirms an ACTIVE limited company, PLC or LLP (PECR "corporate subscribers").
Sole traders and ordinary partnerships are never emailed. Webmail and public-sector addresses are skipped.

What it sends: one email (branded HTML with a plain-text version) naming a live tender in their sector and
area, and at most one follow-up 5 working days later. Every email names the company, gives its postal address
and an opt-out link.

Never the same people twice: each company, email address and email domain gets one first email and at most
one follow-up, EVER. Once someone is emailed, one-way codes of their email, domain and company number go on a
permanent suppression list that survives the 12-month clean-up, so a company that wins another contract later
is not contacted again. Replies, opt-outs and bounces cancel the follow-up. Companies that already subscribe
(any colleague at the same domain) are never cold-emailed. The daily cap counts every email sent that day,
however many times the job runs, and a contact is marked as emailed before the send, so a failure can only
ever mean one email too few, never one too many.

It only sends when ALL of these are true (otherwise it explains what's missing and exits quietly):
  * repo variable OUTREACH_ENABLED = true
  * repo variable OUTREACH_APPROVED_VERSION = TEMPLATE_VERSION below (editing the wording means re-approving it)
  * COMPANY_LINE and POSTAL_ADDRESS are set (company name, number and address go in every email)
  * the outreach mailbox secrets are set (Microsoft Graph app; see README)
  * outreach is not paused (it pauses itself if bounces or opt-outs run high)

Commands:
    python outreach.py check [--limit N]   verify new contacts with Companies House
    python outreach.py inbox               read the outreach mailbox: replies, opt-outs, bounces
    python outreach.py send [--dry-run]    send today's batch (dry run: preview emails go to OWNER_EMAIL only)
    python outreach.py status              counts by status
    python outreach.py prune               delete contacts older than 12 months who never replied (monthly)
    python outreach.py resume              clear an automatic pause

Logs from a public repo are public, so this script never prints names or email addresses.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr

import engine

TEMPLATE_VERSION = "v2"   # bump whenever the wording below changes; the owners approve by setting OUTREACH_APPROVED_VERSION
HARD_DAILY_MAX = 50       # never more than this per day, whatever the setting says
FOLLOW_UP_AFTER_DAYS = 7  # calendar days (= 5 working days)
WEBMAIL = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "live.co.uk",
           "msn.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com",
           "aol.co.uk", "btinternet.com", "btopenworld.com", "sky.com", "virginmedia.com", "ntlworld.com",
           "talktalk.net", "tiscali.co.uk", "blueyonder.co.uk", "protonmail.com", "proton.me", "gmx.com", "gmx.co.uk",
           "mail.com", "zoho.com", "yandex.com"}
PUBLIC_SECTOR = (".gov.uk", ".nhs.uk", "nhs.net", ".sch.uk", ".ac.uk", ".police.uk", ".mod.uk", ".parliament.uk")
# Companies House company types that are corporate bodies (PECR corporate subscribers).
CORPORATE_TYPES = {"ltd", "plc", "llp", "private-limited-guarant-nsc", "private-limited-guarant-nsc-limited-exemption",
                   "private-limited-shares-section-30-exemption", "private-unlimited", "private-unlimited-nsc"}
GENERIC_NAMES = re.compile(r"\b(team|tender|tenders|bid|bids|office|sales|admin|accounts|contracts?|department|dept|"
                           r"manager|director|ltd|limited|plc|llp|services|info|enquiries|reception)\b", re.I)


def env(k, default=""):
    return os.environ.get(k, default).strip()


def mask(email: str) -> str:
    """For logs: never print an address."""
    return "***@" + email.split("@")[-1][:2] + "…" if "@" in (email or "") else "***"


def domain(email: str) -> str:
    return (email or "").rsplit("@", 1)[-1].lower()


def keys_for(c: dict) -> set[str]:
    """Permanent suppression codes for a contact: email, email domain, company number (one-way SHA-256)."""
    h = lambda v: hashlib.sha256(v.encode()).hexdigest()
    keys = {"e:" + h(c["email"].lower()), "c:" + h(str(c["company_number"]).upper())}
    if domain(c["email"]) not in WEBMAIL:
        keys.add("d:" + h(domain(c["email"])))
    return keys


def screen(email: str) -> str | None:
    """Reason to skip an address before any lookup, or None."""
    d = domain(email)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""):
        return "invalid email"
    if d in WEBMAIL:
        return "webmail address (may be an individual)"
    if d.endswith(PUBLIC_SECTOR) or d in {p.lstrip(".") for p in PUBLIC_SECTOR}:
        return "public sector address"
    return None


# ---------------------------------------------------------------- Companies House

def companies_house(number: str) -> dict:
    key = env("COMPANIES_HOUSE_API_KEY")
    auth = base64.b64encode(f"{key}:".encode()).decode()
    try:
        return engine.http_json(f"https://api.company-information.service.gov.uk/company/{urllib.parse.quote(number)}",
                                headers={"Authorization": f"Basic {auth}"}, tries=3) or {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def check(s, limit: int = 300) -> dict:
    """Verify new contacts: corporate body, active, sensible address. Marks 'checked' or 'skipped'."""
    if not env("COMPANIES_HOUSE_API_KEY"):
        return {"skipped": "COMPANIES_HOUSE_API_KEY not set"}
    subs = {x["email"].lower() for x in s.all_subscribers()}
    suppressed = s.suppression_keys()
    out = {"checked": 0, "skipped": 0, "converted": 0, "already_contacted": 0}
    for c in s.outreach_rows(["new"])[:limit]:
        if c["email"] in subs:
            s.outreach_update(c["id"], {"status": "converted"}); out["converted"] += 1; continue
        if keys_for(c) & suppressed:
            s.outreach_update(c["id"], {"status": "skipped", "skip_reason": "already contacted (permanent suppression list)"})
            out["already_contacted"] += 1; continue
        reason = screen(c["email"])
        if not reason:
            info = companies_house(c["company_number"])
            ctype, status = info.get("type"), info.get("company_status")
            if not info:
                reason = "not found at Companies House"
            elif ctype not in CORPORATE_TYPES:
                reason = f"not a limited company or LLP ({ctype})"
            elif status != "active":
                reason = f"company not active ({status})"
            time.sleep(0.6)  # Companies House allows 600 requests per 5 minutes
        fields = {"status": "skipped", "skip_reason": reason} if reason else {"status": "checked"}
        if not reason:
            fields["company_type"] = ctype
        s.outreach_update(c["id"], fields)
        out["skipped" if reason else "checked"] += 1
    return out


# ---------------------------------------------------------------- Microsoft Graph (outreach mailbox)

def graph_token() -> str:
    body = urllib.parse.urlencode({"client_id": env("MS_CLIENT_ID"), "client_secret": env("MS_CLIENT_SECRET"),
                                   "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}).encode()
    r = engine.http_json(f"https://login.microsoftonline.com/{env('MS_TENANT_ID')}/oauth2/v2.0/token", body,
                         {"Content-Type": "application/x-www-form-urlencoded"})
    return r["access_token"]


def graph_send(token: str, msg: EmailMessage) -> None:
    """sendMail with a MIME body, so List-Unsubscribe and plain text survive as written."""
    req = urllib.request.Request(
        f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(env('OUTREACH_MAILBOX'))}/sendMail",
        data=base64.b64encode(msg.as_bytes()), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=60):
        pass


def graph_messages(token: str, since: str) -> list[dict]:
    q = urllib.parse.urlencode({"$filter": f"receivedDateTime ge {since}", "$orderby": "receivedDateTime",
                                "$select": "from,subject,bodyPreview,receivedDateTime", "$top": "50"})
    url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(env('OUTREACH_MAILBOX'))}/mailFolders/inbox/messages?{q}"
    out = []
    while url:
        page = engine.http_json(url, headers={"Authorization": f"Bearer {token}"})
        out += page.get("value", [])
        url = page.get("@odata.nextLink")
    return out


# ---------------------------------------------------------------- inbox: replies, opt-outs, bounces

STOP_WORDS = re.compile(r"\b(unsubscribe|remove me|stop|opt[ -]?out|no thanks|not interested|don'?t (email|contact))\b", re.I)
BOUNCE = re.compile(r"(undeliverable|delivery (status notification|has failed)|mail delivery (failed|subsystem)|returned mail)", re.I)


def classify(msg: dict, known: dict) -> tuple[str, dict] | None:
    """(new status, contact) for one inbox message, or None if it isn't about a contact."""
    sender = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
    text = f"{msg.get('subject') or ''} {msg.get('bodyPreview') or ''}"
    if BOUNCE.search(msg.get("subject") or "") or re.search(r"(postmaster|mailer-daemon|microsoftexchange)", sender):
        for addr in re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", text.lower()):
            if addr in known:
                return "bounced", known[addr]
        return None
    if sender in known:
        return ("opted_out" if STOP_WORDS.search(text) else "replied"), known[sender]
    return None


def inbox(s) -> dict:
    if not all(env(k) for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "OUTREACH_MAILBOX")):
        return {"skipped": "mailbox secrets not set"}
    since = s.get_state("outreach_inbox_since") or (dt.datetime.utcnow() - dt.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    started = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    known = {c["email"]: c for c in s.outreach_rows(["emailed", "followed_up", "replied"])}
    out = {"replied": 0, "opted_out": 0, "bounced": 0}
    for m in graph_messages(graph_token(), since):
        hit = classify(m, known)
        if hit and hit[1]["status"] != hit[0]:
            status, c = hit
            s.outreach_update(c["id"], {"status": status, "responded_at": engine.now()})
            s.suppress(keys_for(c), status)
            c["status"] = status; out[status] += 1
    s.set_state("outreach_inbox_since", started)
    return out


def health(s) -> str | None:
    """Reason to pause, or None. Looks at the last 100 emailed contacts."""
    sent = sorted((c for c in s.outreach_rows() if c.get("emailed_at")), key=lambda c: c["emailed_at"], reverse=True)[:100]
    if len(sent) < 20:
        return None
    n = len(sent)
    bounced = sum(c["status"] == "bounced" for c in sent) / n
    opted = sum(c["status"] == "opted_out" for c in sent) / n
    if bounced > 0.05:
        return f"bounce rate {bounced:.0%} over the last {n} emails (limit 5%): check list quality"
    if opted > 0.04:
        return f"opt-out rate {opted:.0%} over the last {n} emails (limit 4%): review the wording and targeting"
    return None


# ---------------------------------------------------------------- choosing who and what

def first_name(contact_name: str | None) -> str | None:
    parts = (contact_name or "").replace(".", " ").split()
    if len(parts) >= 2 and not GENERIC_NAMES.search(contact_name) and parts[0].isalpha() and len(parts[0]) > 1:
        return parts[0].capitalize()
    return None


def hook(notices: list[dict], pack: str, region: str | None, today: dt.date) -> tuple[dict, int, str] | None:
    """(best live tender, how many are open, where) for this sector and area."""
    soon = (today + dt.timedelta(days=7)).isoformat()
    pools = []
    if region:
        pools.append((place(region), [n for n in notices if engine.topical(n, pack) and engine.in_regions(n, [region])]))
    pools.append(("the UK", [n for n in notices if engine.topical(n, pack)]))
    for where, pool in pools:
        live = [n for n in pool if (n["deadline"] or "")[:10] >= soon]
        if live:
            return max(live, key=lambda n: float(n["value"] or 0)), len(pool), where
    return None


THE = {"UKC", "UKD", "UKF", "UKG", "UKH", "UKJ", "UKK"}  # "the North East", but "London", "Wales"


def place(region: str | None) -> str:
    return "the UK" if not region else ("the " if region in THE else "") + engine.REGIONS[region]


def sector_word(pack: str) -> str:
    return engine.sector_phrase({"pack": pack}).replace(" tenders", "")


def parts(c: dict, tender: dict, count: int, where: str, follow_up: bool) -> dict:
    """The wording, once, so the HTML and plain-text versions always say the same thing.
    Changing any wording or layout means bumping TEMPLATE_VERSION (the owners then approve it again)."""
    sector = sector_word(c["pack"])
    name = first_name(c.get("contact_name"))
    close = engine._ts(tender["deadline"]).strftime("%-d %B %Y") if tender.get("deadline") else "date not stated"
    p = {
        "hello": f"Hi {name}," if name else "Hello,",
        "tender": {"title": tender["title"], "buyer": tender.get("buyer") or "", "close": close, "url": tender["url"],
                   "value": "" if tender.get("value") in (None, "") else engine.money(tender["value"])},
        "cta": ("Try it free for 14 days", f"{engine.SITE_URL}/?sector={c['pack']}#signup"),
        "cta_note": "No card needed. Cancel any time.",
        "sig": [x for x in (env("OUTREACH_SENDER_NAME"), "Bid Canary") if x],
        "legal": [x for x in (env("COMPANY_LINE"), env("POSTAL_ADDRESS")) if x],
        "why": f"You're getting this because {c['company_name']} is named as a supplier on a public contract award notice.",
        "optout": f"{engine.SITE_URL}/optout.html?t={c['optout_token']}",
    }
    if not follow_up:
        p["subject"] = f"{count} open {sector} tenders in {where}"
        p["before"] = [f'Public records show {c["company_name"]} recently won "{c["award_title"]}". Congratulations.',
                       "There's a new one you might want to look at:"]
        p["after"] = [f"It's one of {count} open {sector} tenders in {where} right now. Bid Canary sends every new one "
                      "to your inbox each morning, with a plain-English summary of each."]
        p["closing"] = []
    else:
        p["subject"] = f"Following up: {sector} tenders in {where}"
        p["before"] = ["Just following up in case my last email got buried. This one is still open:"]
        p["after"] = [f"If it's useful, the free trial sends you every new {sector} tender in {where} each morning."]
        p["closing"] = ["I won't email again after this."]
    return p


def render_text(p: dict) -> str:
    t = p["tender"]
    meta = " · ".join(x for x in (t["buyer"], t["value"], f"closes {t['close']}") if x)
    lines = [p["hello"], "", *sum(([x, ""] for x in p["before"]), []),
             f'"{t["title"]}"', meta, t["url"], "",
             *sum(([x, ""] for x in p["after"]), []),
             f"{p['cta'][0]} ({p['cta_note']}):", p["cta"][1], "",
             *sum(([x, ""] for x in p["closing"]), []),
             *p["sig"], *p["legal"], "",
             f"{p['why']} If you'd rather not hear from us, use this link and we won't email you again: "
             f"{p['optout']} (or just reply \"no thanks\")."]
    return "\n".join(lines) + "\n"


def render_html(p: dict) -> str:
    """Branded but light: no images, no tracking pixel, table layout so Outlook renders it."""
    e = html.escape
    t = p["tender"]
    para = lambda x: f'<p style="margin:0 0 16px">{e(x)}</p>'
    meta = " &middot; ".join(e(x) for x in (t["buyer"], t["value"]) if x)
    return f"""<!DOCTYPE html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{e(p['subject'])}</title></head>
<body style="margin:0;padding:0;background:#F6F5EF">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#F6F5EF"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:580px;background:#ffffff;border:1px solid #E3E1D6;border-radius:14px">
<tr><td style="padding:22px 32px 0">
  <table role="presentation" cellspacing="0" cellpadding="0"><tr>
    <td style="width:12px;height:12px;background:#FFD43B;border-radius:3px;font-size:0;line-height:0">&nbsp;</td>
    <td style="padding-left:9px;font:800 17px/1 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0E1512;letter-spacing:-0.3px">Bid Canary</td>
  </tr></table>
</td></tr>
<tr><td style="padding:22px 32px 8px;font:15px/1.6 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0E1512">
  <p style="margin:0 0 16px">{e(p['hello'])}</p>
  {''.join(para(x) for x in p['before'])}
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 18px;border:1px solid #E3E1D6;border-left:4px solid #FFD43B;border-radius:8px;background:#FBFAF6">
  <tr><td style="padding:14px 16px">
    <div style="font:700 15px/1.4 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0E1512">{e(t['title'])}</div>
    <div style="font:13px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#3D4A44;margin-top:4px">{meta}</div>
    <div style="font:600 13px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0E1512;margin-top:2px">Closes {e(t['close'])}</div>
    <div style="margin-top:8px"><a href="{e(t['url'])}" style="font:600 13px -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0F3D2E">Read the full notice on Find a Tender &rarr;</a></div>
  </td></tr></table>
  {''.join(para(x) for x in p['after'])}
  <table role="presentation" cellspacing="0" cellpadding="0" style="margin:4px 0 6px"><tr>
    <td style="background:#FFD43B;border-radius:999px"><a href="{e(p['cta'][1])}" style="display:inline-block;padding:12px 22px;font:700 15px -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0E1512;text-decoration:none">{e(p['cta'][0])}</a></td>
  </tr></table>
  <p style="margin:0 0 18px;font-size:13px;color:#5B6762">{e(p['cta_note'])}</p>
  {''.join(para(x) for x in p['closing'])}
  <p style="margin:0">{'<br>'.join(e(x) for x in p['sig'])}</p>
</td></tr>
<tr><td style="padding:18px 32px 24px;border-top:1px solid #E3E1D6;font:12px/1.55 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#5B6762">
  {('<p style="margin:0 0 8px">' + '<br>'.join(e(x) for x in p['legal']) + '</p>') if p['legal'] else ''}
  <p style="margin:0">{e(p['why'])} If you'd rather not hear from us, <a href="{e(p['optout'])}" style="color:#5B6762">click here and we won't email you again</a>, or just reply &ldquo;no thanks&rdquo;.</p>
</td></tr>
</table>
</td></tr></table>
</body></html>"""


def render(c: dict, tender: dict, count: int, where: str, follow_up: bool) -> tuple[str, str, str]:
    """(subject, plain text, html)."""
    p = parts(c, tender, count, where, follow_up)
    return p["subject"], render_text(p), render_html(p)


def build_message(c: dict, subject: str, body: str, html_body: str | None = None, to: str | None = None) -> EmailMessage:
    m = EmailMessage()
    mbx = env("OUTREACH_MAILBOX")
    m["From"] = formataddr((env("OUTREACH_SENDER_NAME") or "Bid Canary", mbx))
    m["To"] = to or c["email"]
    m["Subject"] = subject
    m["List-Unsubscribe"] = f"<{engine.SITE_URL}/optout.html?t={c['optout_token']}>, <mailto:{mbx}?subject=unsubscribe>"
    m.set_content(body)
    if html_body:
        m.add_alternative(html_body, subtype="html")
    return m


def blockers() -> list[str]:
    missing = []
    if env("OUTREACH_ENABLED").lower() != "true":
        missing.append("repo variable OUTREACH_ENABLED is not 'true'")
    if env("OUTREACH_APPROVED_VERSION") != TEMPLATE_VERSION:
        missing.append(f"wording {TEMPLATE_VERSION} not approved (set repo variable OUTREACH_APPROVED_VERSION={TEMPLATE_VERSION})")
    for k in ("COMPANY_LINE", "POSTAL_ADDRESS", "OUTREACH_MAILBOX", "OUTREACH_SENDER_NAME", "MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"):
        if not env(k):
            missing.append(f"{k} not set")
    return missing


def sent_on(rows: list[dict], day: dt.date) -> int:
    d = day.isoformat()
    return sum((c.get("emailed_at") or "").startswith(d) for c in rows) + sum((c.get("followed_up_at") or "").startswith(d) for c in rows)


def plan_batch(s, today: dt.date) -> list[tuple[dict, bool]]:
    """Today's (contact, is_follow_up) list: follow-ups first, then new contacts.
    Each company, address and domain gets one first email and one follow-up, ever."""
    rows = s.outreach_rows()
    limit = min(int(env("OUTREACH_DAILY_LIMIT") or 25), HARD_DAILY_MAX) - sent_on(rows, today)  # counts earlier runs today
    if limit <= 0:
        return []
    packs = [p.strip() for p in (env("OUTREACH_PACKS") or "cleaning").split(",") if p.strip() in engine.PACKS]
    suppressed = s.suppression_keys()
    subs = {x["email"].lower() for x in s.all_subscribers()}
    sub_domains = {domain(e) for e in subs} - WEBMAIL
    responded = {domain(c["email"]) for c in rows if c["status"] in ("opted_out", "bounced", "replied", "converted")}
    ever_contacted = {domain(c["email"]) for c in rows if c.get("emailed_at")}
    cutoff = (dt.datetime.combine(today, dt.time(), dt.timezone.utc) - dt.timedelta(days=FOLLOW_UP_AFTER_DAYS)).isoformat()
    ups = [(c, True) for c in rows
           if c["status"] == "emailed" and c.get("emailed_at") and not c.get("followed_up_at") and not c.get("responded_at")
           and c["emailed_at"] <= cutoff and domain(c["email"]) not in responded | sub_domains and c["email"] not in subs]
    fresh, seen_domains, seen_emails = [], set(), set()
    for c in sorted((c for c in rows if c["status"] == "checked" and c.get("pack") in packs),
                    key=lambda c: c.get("created_at") or "", reverse=True):
        d = domain(c["email"])
        if (keys_for(c) & suppressed or d in ever_contacted or d in responded or d in sub_domains or d in seen_domains
                or c["email"] in subs or c["email"] in seen_emails or screen(c["email"])):
            continue
        seen_domains.add(d); seen_emails.add(c["email"]); fresh.append((c, False))
    return (ups + fresh)[:limit]


def send(s, dry_run: bool = False, today: dt.date | None = None, pause_between=(60, 180)) -> dict:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    if today.weekday() >= 5:
        return {"skipped": "weekend"}
    paused = s.get_state("outreach_paused")
    if paused and not dry_run:
        return {"skipped": f"paused: {paused}. Run the Outreach workflow with 'resume' after fixing it."}
    missing = blockers()
    if missing and not dry_run:
        return {"skipped": "not switched on", "missing": missing}
    reason = health(s)
    if reason and not dry_run:
        s.set_state("outreach_paused", reason)
        engine.notify_owners("Bid Canary: outreach paused", f"Outreach paused itself: {reason}.\n"
                             "Fix the cause, then run the Outreach workflow with 'resume' ticked.")
        return {"skipped": f"paused: {reason}"}
    notices = s.open_notices()
    batch = plan_batch(s, today)
    have_mailbox = all(env(k) for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "OUTREACH_MAILBOX"))
    token, token_at = (graph_token(), time.time()) if have_mailbox else (None, 0)
    out = {"sent": 0, "follow_ups": 0, "no_live_tender": 0, "failed": 0, "previews_to_owner": 0, "missing": missing}
    previews = []
    for c, follow_up in batch:
        h = hook(notices, c["pack"], c.get("region"), today)
        if not h:
            out["no_live_tender"] += 1; continue
        subject, body, html_body = render(c, *h, follow_up=follow_up)
        if dry_run:
            previews.append((c, subject, body, html_body)); continue
        # Record first, send second: if anything fails after this, the contact gets one email too few, never one too many.
        stamp = engine.now() if today == dt.datetime.now(dt.timezone.utc).date() else f"{today.isoformat()}T09:00:00+00:00"
        s.outreach_update(c["id"], {"status": "followed_up", "followed_up_at": stamp} if follow_up
                          else {"status": "emailed", "emailed_at": stamp, "subject": subject[:200]})
        s.suppress(keys_for(c), "emailed")
        if time.time() - token_at > 45 * 60:  # Graph tokens last about an hour; a full batch can take longer
            token, token_at = graph_token(), time.time()
        try:
            graph_send(token, build_message(c, subject, body, html_body))
        except urllib.error.HTTPError as e:
            if e.code in (429,) or e.code >= 500:
                raise  # Microsoft is throttling or down: stop for today (this contact stays marked, so no retry)
            s.outreach_update(c["id"], {"status": "skipped", "skip_reason": f"send refused ({e.code})"})
            out["failed"] += 1; continue
        out["follow_ups" if follow_up else "sent"] += 1
        time.sleep(random.uniform(*pause_between))  # spread sends out like a person would
    if dry_run and previews and token:
        # Preview goes ONLY to the owners' inbox, never to the contact, and never into the public log.
        for c, subject, body, html_body in previews[:3]:
            graph_send(token, build_message(c, f"[PREVIEW, not sent to {c['company_name']}] {subject}", body, html_body,
                                            to=engine.OWNER_EMAIL))
            out["previews_to_owner"] += 1
    out["would_send"] = len(previews) if dry_run else None
    return out


def prune(s) -> str:
    """Retention: remove contacts we never heard back from after 12 months (database function prune_outreach)."""
    if isinstance(s, engine.SupabaseStore):
        s._req("POST", "rpc/prune_outreach", {})
        return "pruned"
    return "skipped (local database)"


def status(s) -> dict:
    counts = {}
    for c in s.outreach_rows():
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return {"by_status": counts, "paused": s.get_state("outreach_paused"), "template": TEMPLATE_VERSION,
            "blockers": blockers()}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    s = engine.store()
    if cmd == "check":
        lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 300
        print(json.dumps(check(s, lim)))
    elif cmd == "inbox":
        print(json.dumps(inbox(s)))
    elif cmd == "send":
        r = send(s, dry_run="--dry-run" in sys.argv)
        print(json.dumps(r))
        if dt.date.today().weekday() == 0 and "--dry-run" not in sys.argv:
            engine.notify_owners("Bid Canary: weekly outreach summary", json.dumps(status(s), indent=1))
    elif cmd == "prune":
        print(prune(s))
    elif cmd == "status":
        print(json.dumps(status(s), indent=1))
    elif cmd == "resume":
        s.set_state("outreach_paused", ""); print("outreach resumed")
    else:
        print(__doc__)
