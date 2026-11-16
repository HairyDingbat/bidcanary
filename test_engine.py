"""Offline tests using a SYNTHETIC OCDS fixture (not real notices). Run: python test_engine.py"""
import datetime as dt
import json
import os
import tempfile

os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("SUPABASE_URL", None)
import engine  # noqa: E402

future = (dt.date.today() + dt.timedelta(days=20)).isoformat() + "T12:00:00Z"
past = (dt.date.today() - dt.timedelta(days=2)).isoformat() + "T12:00:00Z"


def rel(ocid, title, cpv, region, value, deadline):
    return {"ocid": ocid, "id": ocid + "-1", "tag": ["tender"], "buyer": {"name": "Test Council"},
            "tender": {"title": title, "description": "Synthetic test notice. " * 3, "value": {"amount": value},
                       "tenderPeriod": {"endDate": deadline}, "classification": {"scheme": "CPV", "id": cpv},
                       "items": [{"deliveryAddresses": [{"region": region}]}]}}


fixture = [rel(f"ocds-c-{i}", f"Office cleaning lot {i}", "90919200", "UKD3", 100000 + i, future) for i in range(7)] + [
    rel("ocds-g-1", "Manned guarding at depots", "79713000", "UKD3", 300000, future),
    rel("ocds-c-x", "Library cleaning (expired)", "90911200", "UKD3", 50000, past),
    rel("ocds-c-k", "School cleaning in Kent", "90919300", "UKJ4", 80000, future),
    rel("ocds-it", "Laptop supply", "30213100", "UKD3", 90000, future),
]

s = engine.SQLiteStore(tempfile.mktemp(suffix=".db"))
s.upsert_notices([engine.parse_release(r) for r in fixture])
s.insert_subscriber({"id": "free1", "email": "free@example.com", "pack": "cleaning", "regions": "UKD",
                     "plan": "free", "status": "active", "unsub_token": "t1", "created_at": "now"})
s.insert_subscriber({"id": "paid1", "email": "paid@example.com", "pack": "cleaning", "regions": "UKD",
                     "plan": "free", "status": "active", "unsub_token": "t2", "created_at": "now"})

monday = dt.date(2026, 11, 16)
tuesday = monday + dt.timedelta(days=1)

# Stripe sync upgrades a matching email; an unknown payer is flagged for follow-up.
rep = engine.apply_plans(s, {"paid@example.com": "starter", "stranger@example.com": "starter"})
assert rep["upgraded"] == ["paid@example.com"] and rep["paid_without_profile"] == ["stranger@example.com"], rep

# Tuesday: only the paid subscriber gets an email; free users wait for Monday.
out = engine.digest(s, dry_run=True, today=tuesday)
assert set(out) == {"paid@example.com"}, out
assert out["paid@example.com"]["count"] == 7  # 7 open North West cleaning tenders; expired + Kent + IT excluded
assert "Synthetic test notice" in out["paid@example.com"]["html"]  # summary shown to paid

# Monday: free subscriber gets top 5 plus an upsell for the other 2, without summaries.
out = engine.digest(s, dry_run=True, today=monday)
f = out["free@example.com"]
assert f["count"] == 7 and "2 more matching tenders" in f["html"] and "Synthetic test notice" not in f["html"]
assert "unsubscribe.html?t=t1" in f["html"]

# No repeats.
assert engine.digest(s, dry_run=True, today=monday) == {}

# Cancelling in Stripe drops the plan back to free.
rep = engine.apply_plans(s, {})
assert rep["downgraded"] == ["paid@example.com"], rep

# Aggregate data for the site's area checker.
path = tempfile.mktemp(suffix=".json")
c = engine.build_counts(s, path)
assert c["packs"]["cleaning"]["regions"]["UKD"]["count"] == 7
assert c["packs"]["cleaning"]["regions"]["UKJ"]["count"] == 1
assert c["packs"]["security"]["regions"]["UKD"]["count"] == 1
assert "email" not in json.dumps(c)
print("all tests passed")
