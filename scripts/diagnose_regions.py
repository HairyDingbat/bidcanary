"""Diagnose location data: what region info Find a Tender actually provides."""
import collections
import datetime as dt
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine  # noqa: E402

end = dt.datetime.utcnow()
start = end - dt.timedelta(days=4)
url = f"{engine.FTS_URL}?{urllib.parse.urlencode({'updatedFrom': start.strftime('%Y-%m-%dT%H:%M:%S'), 'updatedTo': end.strftime('%Y-%m-%dT%H:%M:%S'), 'limit': 100})}"
rels = []
while url and len(rels) < 600:
    pkg = engine.http_json(url)
    rels += [r for r in pkg.get("releases", []) if any(t in r.get("tag", []) for t in ("tender", "tenderUpdate"))]
    url = (pkg.get("links") or {}).get("next")

deliv = collections.Counter(); buyer_region = collections.Counter(); has = collections.Counter()
samples = []
for r in rels:
    t = r.get("tender", {})
    d = [a for it in t.get("items", []) for a in it.get("deliveryAddresses", [])]
    dl = [a.get("region") for a in d if a.get("region")]
    for x in dl: deliv[x[:4]] += 1
    has["delivery_region"] += bool(dl)
    has["delivery_any_address"] += bool(d)
    has["tender_deliveryLocations"] += bool(t.get("deliveryLocations"))
    b = next((p for p in r.get("parties", []) if "buyer" in p.get("roles", [])), {})
    ba = b.get("address", {})
    buyer_region[(ba.get("region") or "")[:4]] += 1
    has["buyer_postcode"] += bool(ba.get("postalCode"))
    if len(samples) < 12:
        samples.append({"title": t.get("title", "")[:70], "delivery": d[:2], "deliveryLocations": t.get("deliveryLocations", [])[:2],
                        "buyer": b.get("name"), "buyer_address": {k: ba.get(k) for k in ("locality", "region", "postalCode", "countryName")}})
print(json.dumps({"tender_releases": len(rels), "has": dict(has), "delivery_region_prefixes": deliv.most_common(30),
                  "buyer_region_prefixes": buyer_region.most_common(30), "samples": samples}, indent=1))
