"""Market scan: how many relevant tenders (and winning suppliers) appear on Find a Tender.

Usage: python scripts/market_scan.py [days]   (default 180)
Prints a JSON summary and writes scan.json. Read-only; stores nothing.
"""
import collections
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine  # noqa: E402

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 180


def get(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BidCanary-scan/0.1"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(int(e.headers.get("Retry-After") or 2 ** (i + 1)))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if i < tries - 1:
                time.sleep(2 ** (i + 1)); continue
            raise


def band(v):
    if v is None: return "not stated"
    v = float(v)
    if v < 25000: return "under £25k"
    if v < 140000: return "£25k–£140k"
    if v < 1000000: return "£140k–£1m"
    return "over £1m"


end = dt.datetime.utcnow().replace(microsecond=0)
start = end - dt.timedelta(days=DAYS)
tenders, awards = {}, {}
total_releases = 0
cur = start
while cur < end:
    nxt = min(cur + dt.timedelta(days=3), end)
    url = f"{engine.FTS_URL}?{urllib.parse.urlencode({'updatedFrom': cur.strftime('%Y-%m-%dT%H:%M:%S'), 'updatedTo': nxt.strftime('%Y-%m-%dT%H:%M:%S'), 'limit': 100})}"
    while url:
        pkg = get(url)
        for rel in pkg.get("releases", []):
            total_releases += 1
            tags = rel.get("tag", [])
            ocid = rel.get("ocid")
            if not ocid:
                continue
            row = engine.parse_release(rel)
            if row:
                tenders[ocid] = row
            if any(t in tags for t in ("award", "awardUpdate", "contract")):
                t = rel.get("tender", {})
                fake = {"title": t.get("title", ""), "description": t.get("description") or "", "cpvs": []}
                for c in [t.get("classification") or {}] + [x for it in t.get("items", []) for x in [it.get("classification") or {}] + it.get("additionalClassifications", [])]:
                    if c.get("scheme", "").upper() == "CPV" and c.get("id"):
                        fake["cpvs"].append(str(c["id"]))
                sups = {p.get("name") for p in rel.get("parties", []) if "supplier" in p.get("roles", []) and p.get("name")}
                for a in rel.get("awards", []):
                    sups |= {s.get("name") for s in a.get("suppliers", []) if s.get("name")}
                awards[ocid] = (fake, sups)
        url = (pkg.get("links") or {}).get("next")
        time.sleep(0.2)
    cur = nxt

out = {"window_days": DAYS, "from": start.isoformat(), "to": end.isoformat(),
       "releases_scanned": total_releases, "unique_open_opportunities": len(tenders), "packs": {}}
any_pack = set()
for pack in engine.PACKS:
    hits = [n for n in tenders.values() if engine.topical(n, pack)]
    any_pack |= {n["ocid"] for n in hits}
    by_month = collections.Counter((n["seen_at"] or "")[:7] for n in hits)
    by_band = collections.Counter(band(n["value"]) for n in hits)
    by_region = collections.Counter((n["regions"][0][:3] if n["regions"] else "none") for n in hits)
    buyers = {n["buyer"] for n in hits if n["buyer"]}
    win = [s for f, s in awards.values() if engine.topical(f, pack)]
    suppliers = set().union(*win) if win else set()
    out["packs"][pack] = {
        "opportunities": len(hits),
        "per_month_avg": round(len(hits) / (DAYS / 30.4), 1),
        "value_bands": dict(by_band),
        "regions": dict(sorted(by_region.items())),
        "unique_buyers": len(buyers),
        "awards": len(win),
        "unique_winning_suppliers": len(suppliers),
        "sample_titles": [n["title"][:100] for n in sorted(hits, key=lambda n: n["deadline"] or "")[-5:]],
    }
out["any_target_trade"] = len(any_pack)
open("scan.json", "w").write(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
