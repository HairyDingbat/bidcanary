import datetime as dt, json, urllib.parse, urllib.request, collections
base = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
end = dt.datetime.utcnow().replace(microsecond=0)
def get(p):
    url = base + "?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "BidCanary-probe"}), timeout=120) as r:
        return json.loads(r.read())
tot = collections.Counter(); tags = collections.Counter(); ocids = set(); vals = collections.Counter()
for d in range(14):
    s = end - dt.timedelta(days=d + 1); e = end - dt.timedelta(days=d)
    for stage in ("tender", None):
        p = {"publishedFrom": s.isoformat(), "publishedTo": e.isoformat(), "limit": 100}
        if stage: p["stages"] = stage
        try:
            r = get(p)
        except Exception as ex:
            print("ERR", d, stage, ex); continue
        rel = r.get("releases", [])
        tot[stage or "all"] += len(rel)
        if len(rel) >= 100: print("page full on day", d, stage)
        if stage:
            for x in rel:
                ocids.add(x["ocid"]); tags[",".join(x.get("tag", []))] += 1
                v = (x.get("tender", {}).get("value") or {}).get("amount")
                vals["none" if v is None else "<25k" if v < 25000 else "25-140k" if v < 140000 else "140k+"] += 1
print("14 days totals", dict(tot), "unique tender ocids", len(ocids))
print("tags", tags.most_common(8)); print("values", dict(vals))
