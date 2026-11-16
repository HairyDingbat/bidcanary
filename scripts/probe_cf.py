"""Probe the Contracts Finder OCDS search API and print its shape."""
import datetime as dt, json, urllib.parse, urllib.request
end = dt.datetime.utcnow().replace(microsecond=0); start = end - dt.timedelta(days=2)
base = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
for params in ({"publishedFrom": start.isoformat(), "publishedTo": end.isoformat(), "stages": "tender", "limit": 100},
               {"publishedFrom": start.isoformat(), "publishedTo": end.isoformat(), "stages": "tender", "size": 100}):
    url = base + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "BidCanary-probe", "Accept": "application/json"}), timeout=90) as r:
            d = json.loads(r.read())
    except Exception as e:
        print("ERR", url, e); continue
    rels = d.get("releases", [])
    print("URL", url); print("top keys", list(d.keys())); print("links", d.get("links")); print("releases", len(rels))
    regs = {}
    for x in rels:
        for it in x.get("tender", {}).get("items", []):
            for a in it.get("deliveryAddresses", []):
                regs[str(a.get("region"))] = regs.get(str(a.get("region")), 0) + 1
    print("delivery regions", sorted(regs.items(), key=lambda kv: -kv[1])[:25])
    if rels:
        x = rels[0]; t = x.get("tender", {})
        b = next((p for p in x.get("parties", []) if "buyer" in p.get("roles", [])), {})
        print("sample", json.dumps({"ocid": x.get("ocid"), "tag": x.get("tag"), "title": t.get("title"), "value": t.get("value"),
              "tenderPeriod": t.get("tenderPeriod"), "classification": t.get("classification"),
              "items": [{"deliveryAddresses": i.get("deliveryAddresses"), "classification": i.get("classification")} for i in t.get("items", [])[:2]],
              "buyer_address": b.get("address"), "suitability": t.get("suitability"), "documents": [dd.get("url") for dd in t.get("documents", [])[:2]]}, indent=1)[:2500])
    break
