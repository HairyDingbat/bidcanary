"""End-to-end check of the live database security, run from GitHub Actions.

1. The public (publishable) key can add a free signup.
2. The public key cannot read subscribers back, cannot set a paid plan, and cannot read notices.
3. Unsubscribe by token works.
4. The test row is removed with the secret key.
Prints PASS/FAIL lines; exits non-zero on any failure.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

missing = [k for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY") if not os.environ.get(k)]
for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"):
    v = os.environ.get(k, "")
    shape = ("missing" if not v else "https URL" if v.startswith("https://") else
             v.split("_")[0] + "_" + v.split("_")[1] + "_..." if v.startswith("sb_") else "eyJ... (legacy)" if v.startswith("eyJ") else "unexpected format")
    print(f"SECRET {k}: {shape}")
site = os.environ.get("SITE_URL", "")  # SITE_URL check
print("SECRET SITE_URL: " + ("missing" if not site else "correct (https://bidcanary.co.uk)" if site.rstrip("/") == "https://bidcanary.co.uk"
      else "set but not https://bidcanary.co.uk (length %d, starts %s)" % (len(site), site[:8])))
if missing:
    print("FAIL secrets missing or misnamed: " + ", ".join(missing)); sys.exit(1)
URL = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/"
PUB = os.environ["SUPABASE_ANON_KEY"]
SEC = os.environ["SUPABASE_SERVICE_KEY"]


def headers(key, extra=None):
    h = {"apikey": key, "Content-Type": "application/json"}
    if not key.startswith("sb_"):
        h["Authorization"] = f"Bearer {key}"
    return {**h, **(extra or {})}


def call(method, path, key, body=None, extra=None):
    req = urllib.request.Request(URL + path, method=method, headers=headers(key, extra),
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


fails = 0
def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    fails += 0 if ok else 1


email = f"selftest-{uuid.uuid4().hex[:8]}@example.com"
s, b = call("POST", "subscribers", PUB, {"email": email, "pack": "cleaning", "regions": "UKD", "consent_text": "selftest"},
            {"Prefer": "return=minimal"})
check("public key can add a free signup", s in (200, 201), f"{s} {b}")

s, b = call("POST", "subscribers", PUB, {"email": email, "pack": "cleaning", "regions": "UKD"}, {"Prefer": "return=minimal"})
check("duplicate email is rejected with 409", s == 409, f"{s} {b}")

s, b = call("POST", "subscribers", PUB, {"email": "kw-" + email, "pack": "construction", "regions": "UKD,UKE", "keywords": "roofing, solar", "consent_text": "selftest"},
            {"Prefer": "return=minimal"})
check("public key can add a new sector with keywords", s in (200, 201), f"{s} {b}")

s, b = call("POST", "subscribers", PUB, {"email": "bad-" + email, "pack": "Not A Sector!", "consent_text": "selftest"}, {"Prefer": "return=minimal"})
check("junk sector values are rejected", s >= 400, f"{s} {b}")

s, b = call("GET", "subscribers?select=email", PUB)
check("public key cannot read subscribers", s in (401, 403) or (s == 200 and b == []), f"{s} {b}")

s, b = call("POST", "subscribers", PUB, {"email": "x" + email, "pack": "cleaning", "plan": "pro"}, {"Prefer": "return=minimal"})
check("public key cannot create a paid plan", s >= 400, f"{s} {b}")

s, b = call("GET", "notices?select=ocid&limit=1", PUB)
check("public key cannot read notices", s in (401, 403) or (s == 200 and b == []), f"{s} {b}")

s, b = call("POST", "rpc/prune_old_notices", PUB, {})
check("public key cannot run the cleanup function", s >= 400, f"{s} {b}")

s, b = call("GET", f"subscribers?email=eq.{email}&select=id,plan,status,unsub_token", SEC)
row = b[0] if s == 200 and b else None
check("new signup starts unconfirmed (free, pending)", bool(row) and row["plan"] == "free" and row["status"] == "pending", f"{s} {b}")

s, b = call("POST", "subscribers", PUB, {"email": "y" + email, "pack": "cleaning", "status": "active"}, {"Prefer": "return=minimal"})
check("public key cannot skip email confirmation", s >= 400, f"{s} {b}")

if row:
    s, b = call("POST", "rpc/confirm_subscription", PUB, {"token": str(uuid.uuid4())})
    check("a made-up confirmation token does nothing", s == 200 and b is False, f"{s} {b}")
    s, b = call("POST", "rpc/confirm_subscription", PUB, {"token": row["unsub_token"]})
    check("confirmation link activates the signup", s == 200 and b is True, f"{s} {b}")
    s, b = call("POST", "rpc/confirm_subscription", PUB, {"token": row["unsub_token"]})
    check("confirmation link can't be reused", s == 200 and b is False, f"{s} {b}")
    s, b = call("POST", "rpc/unsubscribe", PUB, {"token": row["unsub_token"]})
    check("unsubscribe link works", s == 200 and b is True, f"{s} {b}")
    s, b = call("POST", "rpc/unsubscribe", PUB, {"token": str(uuid.uuid4())})
    check("a made-up unsubscribe token does nothing", s == 200 and b is False, f"{s} {b}")

if row:
    s, b = call("GET", f"subscribers?email=eq.{email}&select=trial_ends_at,ref_code", SEC)
    r2 = b[0] if s == 200 and b else {}
    check("confirming starts the 14-day trial", bool(r2.get("trial_ends_at")), f"{s} {b}")
    check("every subscriber gets a referral code", bool(r2.get("ref_code")), f"{s} {b}")

s, b = call("POST", "subscribers", PUB, {"email": "ref-" + email, "pack": "cleaning", "referred_by": "abcd1234", "consent_text": "selftest"},
            {"Prefer": "return=minimal"})
check("public key can record a referral code", s in (200, 201), f"{s} {b}")
s, b = call("POST", "subscribers", PUB, {"email": "refbad-" + email, "pack": "cleaning", "referred_by": "<script>"}, {"Prefer": "return=minimal"})
check("junk referral codes are rejected", s >= 400, f"{s} {b}")
for col, val in (("trial_ends_at", "2099-01-01T00:00:00Z"), ("founding", True), ("ref_code", "deadbeef")):
    s, b = call("POST", "subscribers", PUB, {"email": f"{col}-" + email, "pack": "cleaning", col: val}, {"Prefer": "return=minimal"})
    check(f"public key cannot set {col}", s >= 400, f"{s} {b}")
s, b = call("GET", "outreach_contacts?select=email&limit=1", PUB)
check("public key cannot read outreach contacts", s in (401, 403, 404) or (s == 200 and b == []), f"{s} {b}")
s, b = call("POST", "rpc/outreach_optout", PUB, {"token": str(uuid.uuid4())})
check("a made-up outreach opt-out token does nothing", s == 200 and b is False, f"{s} {b}")
s, b = call("POST", "rpc/prune_outreach", PUB, {})
check("public key cannot run the outreach clean-up", s >= 400, f"{s} {b}")

s, b = call("GET", "notices?select=ocid&limit=1", SEC)
check("engine has stored tender notices", s == 200 and bool(b), f"{s} {b}")

s, b = call("DELETE", f"subscribers?email=like.*selftest-*", SEC)
check("test signup cleaned up", s in (200, 204), f"{s} {b}")

print("ALL PASSED" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
