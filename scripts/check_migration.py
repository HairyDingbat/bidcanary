"""Is migration 004 in place? Read-only: checks that its columns and tables answer. Prints no data."""
import os, sys, urllib.request, urllib.error
base = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/"
key = os.environ["SUPABASE_SERVICE_KEY"]
h = {"apikey": key} if key.startswith("sb_") else {"apikey": key, "Authorization": f"Bearer {key}"}
ok = True
for path in ("subscribers?select=trial_ends_at,ref_code,referred_by,founding&limit=0",
             "outreach_contacts?select=id&limit=0", "outreach_suppression?select=key&limit=0"):
    try:
        urllib.request.urlopen(urllib.request.Request(base + path, headers=h), timeout=30).read()
        print("PRESENT", path.split("?")[0], path.split("select=")[1].split("&")[0])
    except urllib.error.HTTPError as e:
        ok = False; print("MISSING", path.split("?")[0], e.code)
print("MIGRATION 004: APPLIED" if ok else "MIGRATION 004: NOT APPLIED")
sys.exit(0 if ok else 1)
