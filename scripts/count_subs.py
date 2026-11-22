"""Count subscribers by status, plan and sector. Prints NO emails or domains (Actions logs are public). Read-only."""
import json, os, urllib.request
from collections import Counter
url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/subscribers?select=pack,plan,status,created_at&order=created_at.asc&limit=10000"
key = os.environ["SUPABASE_SERVICE_KEY"]
h = {"apikey": key}
if not key.startswith("sb_"): h["Authorization"] = "Bearer " + key
rows = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30).read())
print("TOTAL", len(rows))
for f in ("status", "plan", "pack"):
    print(f.upper(), dict(Counter(r[f] for r in rows)))
print("BY DAY", dict(Counter(r["created_at"][:10] for r in rows)))
