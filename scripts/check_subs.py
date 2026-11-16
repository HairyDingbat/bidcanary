"""List subscribers with masked emails: status, plan, sector, timestamps. Read-only."""
import json, os, urllib.request
url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/subscribers?select=email,pack,regions,plan,status,created_at,confirm_sent_at&order=created_at.desc&limit=20"
key = os.environ["SUPABASE_SERVICE_KEY"]
h = {"apikey": key}
if not key.startswith("sb_"): h["Authorization"] = "Bearer " + key
rows = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30).read())
def mask(e):
    u, _, d = e.partition("@"); return u[:2] + "***@" + d
for r in rows:
    print(mask(r["email"]), r["pack"], r["regions"] or "all-UK", r["plan"], r["status"], "created", r["created_at"][:19], "confirm_sent", (r["confirm_sent_at"] or "")[:19])
print("total shown:", len(rows))
