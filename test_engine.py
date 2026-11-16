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

# Double opt-in: a pending sign-up gets one confirmation email and no digest until confirmed.
s.insert_subscriber({"id": "pend1", "email": "pending@example.com", "pack": "cleaning", "regions": "UKD",
                     "plan": "free", "status": "pending", "unsub_token": "t3", "created_at": engine.now()})
s.insert_subscriber({"id": "old1", "email": "stale@example.com", "pack": "cleaning", "regions": "UKD",
                     "plan": "free", "status": "pending", "unsub_token": "t4", "created_at": "2020-01-01T00:00:00+00:00"})
c = engine.send_confirmations(s, dry_run=True)
assert c == {"confirmation_emails": 1}, c                      # stale one deleted, fresh one emailed
assert engine.send_confirmations(s, dry_run=True) == {"confirmation_emails": 0}  # not re-sent
assert "stale@example.com" not in [x["email"] for x in s.all_subscribers()]
assert "confirm.html?t=t3" in engine.render_confirmation({"pack": "cleaning", "unsub_token": "t3"})

# Stripe sync upgrades a matching email; an unknown payer is flagged for follow-up.
rep = engine.apply_plans(s, {"paid@example.com": "starter", "stranger@example.com": "starter"})
assert rep["upgraded"] == ["paid@example.com"] and rep["paid_without_profile"] == ["stranger@example.com"], rep

# Tuesday: only the paid subscriber gets an email; free users wait for Monday.
out = engine.digest(s, dry_run=True, today=tuesday)
assert set(out) == {"paid@example.com"}, out
assert out["paid@example.com"]["count"] == 7  # 7 open North West cleaning tenders; expired + Kent + IT excluded
assert "Synthetic test notice" in out["paid@example.com"]["html"]  # summary shown to paid

# Monday: free subscriber gets one showcase tender (with its summary) and a count of the other 6.
out = engine.digest(s, dry_run=True, today=monday)
assert "pending@example.com" not in out  # unconfirmed sign-ups get nothing
f = out["free@example.com"]
assert f["count"] == 7 and "6 more matching tenders" in f["html"], f["html"]
assert f["html"].count("Closes ") == 1  # exactly one tender shown
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
# Duplicate releases of the same notice in one fetch are collapsed before saving.
class Rec(engine.SQLiteStore):
    def upsert_notices(self, rows):
        assert len({r["ocid"] for r in rows}) == len(rows), "duplicate ocids sent"
        super().upsert_notices(rows)
r2 = Rec(tempfile.mktemp(suffix=".db"))
pkg = {"releases": [rel("dup", "Cleaning v1", "90919200", "UKD3", 1, future), rel("dup", "Cleaning v2", "90919200", "UKD3", 2, future)], "links": {}}
engine.http_json = lambda url, *a, **k: pkg
engine.fetch(r2)
assert [n["title"] for n in r2.open_notices()] == ["Cleaning v2"]
# Region resolution: specific delivery > buyer region > UK-wide.
R = engine.resolve_regions
assert R(["UKD3"], "UKI3") == ["UKD3"]                  # delivery wins
assert R([], "UKN0") == ["UKN0"]                        # no delivery: buyer's region
assert R(["UK"], "UKJ1") == ["UKJ1"]                    # "UK" only: buyer's region
assert R([], None) == ["UK"]                            # nothing known: UK-wide
allr = ["UKC", "UKD", "UKE", "UKF", "UKG", "UKH", "UKI"]
assert R(allr, "UKI3") == ["UK"]                        # spans 6+ regions: UK-wide
n_local = {"regions": ["UKN0"]}; n_nat = {"regions": ["UK"]}
assert engine.in_regions(n_local, ["UKN"]) and not engine.in_regions(n_local, ["UKD"])
assert not engine.in_regions(n_nat, ["UKD"]) and engine.in_regions(n_nat, [])
# A tender with no delivery address but a Belfast buyer must not appear in the North West.
r = rel("ocds-belfast", "Office cleaning Belfast", "90919200", None, 50000, future)
r["tender"]["items"] = []; r["parties"] = [{"name": "Belfast council", "roles": ["buyer"], "address": {"region": "UKN0"}}]
nb = engine.parse_release(r)
assert nb["regions"] == ["UKN0"]
assert engine.in_regions(nb, ["UKN"]) and not engine.in_regions(nb, ["UKD"])
# All sectors + custom keywords.
assert len(engine.PACKS) >= 18 and "construction" in engine.PACKS and "it" in engine.PACKS
road = {"title": "Highway resurfacing works", "description": "Resurfacing of A-roads", "cpvs": ["45233142"], "regions": ["UKD3"]}
assert engine.matches(road, {"pack": "construction", "regions": "UKD"})
assert not engine.matches(road, {"pack": "cleaning", "regions": "UKD"})
assert engine.matches(road, {"pack": "cleaning", "regions": "UKD", "keywords": "resurfacing"})     # keywords widen
assert engine.matches(road, {"pack": "custom", "regions": "", "keywords": "highway, potholes"})
assert not engine.matches(road, {"pack": "custom", "regions": "", "keywords": ""})              # custom with no keywords matches nothing
assert engine.keyword_list("a, ab, roofing, , solar") == ["roofing", "solar"]
assert engine.sector_phrase({"pack": "cleaning"}) == "cleaning tenders"
assert engine.sector_phrase({"pack": "custom"}) == "tenders matching your keywords"
assert engine.sector_phrase({"pack": "it"}) == "IT, software and digital tenders"
print("all tests passed")
