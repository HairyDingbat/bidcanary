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
txt = engine.html_to_text(engine.render_confirmation({"pack": "cleaning", "unsub_token": "t9"}))
assert "Yes, send me tenders (https://bidcanary.co.uk/confirm.html?t=t9)" in txt and "<" not in txt, txt

# ---- 14-day trial: daily digest during the trial, one "trial ended" note, then weekly.
s3 = engine.SQLiteStore(tempfile.mktemp(suffix=".db"))
s3.upsert_notices([engine.parse_release(r) for r in fixture])
s3.insert_subscriber({"id": "tr1", "email": "trial@example.com", "pack": "cleaning", "regions": "UKD", "plan": "free",
                      "status": "active", "unsub_token": "tt", "created_at": "now", "trial_ends_at": "2026-11-20T06:00:00+00:00"})
wed = dt.date(2026, 11, 18)
out = engine.digest(s3, dry_run=True, today=wed)                        # Wednesday, in trial: daily digest
assert out["trial@example.com"]["plan"] == "trial" and out["trial@example.com"]["count"] == 7
assert "Free trial: 2 days left" in out["trial@example.com"]["html"] and "Synthetic test notice" in out["trial@example.com"]["html"]
assert "?ref=" in out["trial@example.com"]["html"]                     # referral link in every digest
assert "£29.99" in out["trial@example.com"]["html"] and "£29 a" not in out["trial@example.com"]["html"]
out = engine.digest(s3, dry_run=True, today=dt.date(2026, 11, 21))     # Saturday, trial over: note only, no digest
assert out == {"_trial_ended": ["trial@example.com"]}, out
assert engine.digest(s3, dry_run=True, today=dt.date(2026, 11, 22)) == {}   # note never repeated
assert "free trial has ended" in engine.render_trial_ended({"pack": "cleaning", "unsub_token": "tt"}).lower()
assert "14 days of daily alerts free" in engine.render_confirmation({"pack": "cleaning", "unsub_token": "x"})

# ---- Founding members (first 50) and referral rewards flagged for the owners.
s4 = engine.SQLiteStore(tempfile.mktemp(suffix=".db"))
s4.insert_subscriber({"id": "r1", "email": "referrer@example.com", "pack": "cleaning", "regions": "", "plan": "starter",
                      "status": "active", "unsub_token": "a", "created_at": "now", "ref_code": "abcd1234", "founding": 1})
s4.insert_subscriber({"id": "r2", "email": "friend@example.com", "pack": "cleaning", "regions": "", "plan": "free",
                      "status": "active", "unsub_token": "b", "created_at": "now", "referred_by": "abcd1234"})
rep = engine.apply_plans(s4, {"referrer@example.com": "starter", "friend@example.com": "starter"})
assert rep["new_founding_members"] == ["friend@example.com"] and rep["founding_members_total"] == 2, rep
assert rep["referral_rewards_due"] == [{"referrer": "referrer@example.com", "new_customer": "friend@example.com"}], rep
rep = engine.apply_plans(s4, {"referrer@example.com": "starter", "friend@example.com": "starter"})
assert rep["referral_rewards_due"] == [] and rep["new_founding_members"] == []   # flagged once only
assert "free month" in engine.owner_report({"referral_rewards_due": [{"referrer": "a@x.com", "new_customer": "b@y.com"}]})
assert engine.log_safe({"a@b.com": {"plan": "free"}, "l": ["c@d.com"]}) == {"***@b.com": {"plan": "free"}, "l": ["***@d.com"]}

# ---- Award notices feed outreach contacts (Companies House number + email required).
award = {"ocid": "ocds-aw-1", "tag": ["award", "contract"], "tender": {"title": "Office cleaning for Test Council",
         "classification": {"scheme": "CPV", "id": "90919200"}}, "awards": [{"title": "Office cleaning contract"}],
         "parties": [{"name": "Sparkle Cleaning Ltd", "roles": ["supplier"], "identifier": {"scheme": "GB-COH", "id": "1234567"},
                      "contactPoint": {"email": "Jane.Smith@sparkle.co.uk", "name": "Jane Smith"}, "address": {"region": "UKD3"}},
                     {"name": "No Number Ltd", "roles": ["supplier"], "contactPoint": {"email": "a@b.co.uk"}},
                     {"name": "Test Council", "roles": ["buyer"], "contactPoint": {"email": "proc@test.gov.uk"}}]}
sup = engine.parse_suppliers(award)
assert len(sup) == 1 and sup[0]["company_number"] == "01234567" and sup[0]["email"] == "jane.smith@sparkle.co.uk", sup
assert sup[0]["pack"] == "cleaning" and sup[0]["region"] == "UKD"
assert engine.parse_suppliers(fixture[0]) == []                         # tender notices name no suppliers

import outreach  # noqa: E402
assert outreach.screen("bob@gmail.com") and outreach.screen("x@leeds.gov.uk") and outreach.screen("y@trust.nhs.net")
assert outreach.screen("ops@sparkle.co.uk") is None
assert outreach.first_name("Jane Smith") == "Jane" and outreach.first_name("Tenders Team") is None and outreach.first_name("Jane") is None
assert outreach.classify({"from": {"emailAddress": {"address": "ops@a.co.uk"}}, "subject": "Re: x", "bodyPreview": "No thanks"},
                         {"ops@a.co.uk": {"id": 1}})[0] == "opted_out"
assert outreach.classify({"from": {"emailAddress": {"address": "ops@a.co.uk"}}, "subject": "Re: x", "bodyPreview": "Tell me more"},
                         {"ops@a.co.uk": {"id": 1}})[0] == "replied"
assert outreach.classify({"from": {"emailAddress": {"address": "postmaster@x.com"}}, "subject": "Undeliverable: hi",
                          "bodyPreview": "Delivery to ops@a.co.uk failed"}, {"ops@a.co.uk": {"id": 1}})[0] == "bounced"

so = engine.SQLiteStore(tempfile.mktemp(suffix=".db"))
so.upsert_notices([engine.parse_release(r) for r in fixture])
mk = lambda i, email, st="checked", **kw: {"id": f"o{i}", "company_number": f"0000000{i}", "company_name": f"Co {i} Ltd",
                                           "email": email, "pack": "cleaning", "region": "UKD", "status": st,
                                           "award_title": "Cleaning contract", "optout_token": f"tok{i}", **kw}
so.outreach_add([mk(1, "ops@one.co.uk"), mk(2, "sales@one.co.uk"),             # same domain: only one is emailed
                 mk(3, "hi@nope.co.uk"), mk(4, "x@nope.co.uk", "opted_out"),    # domain opted out: skipped
                 mk(5, "free@example.com"),                                     # already a subscriber: skipped
                 mk(6, "old@six.co.uk", "emailed", emailed_at="2026-11-02T09:00:00+00:00"),  # due a follow-up
                 mk(7, "new@seven.co.uk", "new")])                              # not checked yet: skipped
so.insert_subscriber({"id": "sx", "email": "free@example.com", "pack": "cleaning", "regions": "", "plan": "free",
                      "status": "active", "unsub_token": "z", "created_at": "now"})
for k in ("OUTREACH_ENABLED", "OUTREACH_APPROVED_VERSION", "MS_TENANT_ID", "OUTREACH_DAILY_LIMIT", "OUTREACH_PACKS"):
    os.environ.pop(k, None)
batch = outreach.plan_batch(so, monday)
assert [(c["id"], f) for c, f in batch] == [("o6", True), ("o1", False)] or [(c["id"], f) for c, f in batch] == [("o6", True), ("o2", False)], batch
r = outreach.send(so, dry_run=False, today=monday)                     # not approved or switched on: sends nothing
assert r["skipped"] == "not switched on" and any("OUTREACH_ENABLED" in m for m in r["missing"]), r
assert outreach.send(so, today=dt.date(2026, 11, 21)) == {"skipped": "weekend"}
c1 = so.outreach_rows(["checked"])[0]
tender, count, where = outreach.hook(so.open_notices(), "cleaning", "UKD", monday)
assert count == 7 and where == "the North West" and tender["value"] == 100006
assert outreach.place("UKI") == "London" and outreach.place(None) == "the UK"
subj, body, hbody = outreach.render(c1, tender, count, where, follow_up=False)
assert subj == "7 open cleaning tenders in the North West"
assert "Public records show Co" in body and "I saw" not in body
assert "optout.html?t=tok" in hbody and "Try it free for 14 days" in hbody and "<img" not in hbody and "Public records show Co" in hbody
assert "optout.html?t=tok" in body and "named as a supplier on a public contract award" in body and "14 days" in body
subj2, body2, _ = outreach.render(c1, tender, count, where, follow_up=True)
assert subj2.startswith("Following up") and "won't email again" in body2 and not subj2.lower().startswith("re:")
msg = outreach.build_message(c1, subj, body, hbody)
assert "optout.html" in msg["List-Unsubscribe"] and msg["To"] == c1["email"]
assert msg.is_multipart() and [x.get_content_type() for x in msg.iter_parts()] == ["text/plain", "text/html"]
evil = dict(c1, company_name="<script>x</script> Ltd")
assert "<script>" not in outreach.render(evil, tender, count, where, False)[2]           # everything escaped
assert outreach.mask("jane@sparkle.co.uk").startswith("***@")

# ---- Never the same people twice: simulate 8 weeks of daily runs (with repeat runs and a clean-up).
sim = engine.SQLiteStore(tempfile.mktemp(suffix=".db"))
sim.upsert_notices([engine.parse_release(r) for r in fixture])
far = (dt.date.today() + dt.timedelta(days=400)).isoformat() + "T12:00:00Z"   # keep a tender open for the whole run
sim.upsert_notices([engine.parse_release(rel("ocds-long", "Long cleaning", "90919200", "UKD3", 5, far))])
contacts = [mk(i, f"person{i % 15}@firm{i % 15}.co.uk") for i in range(1, 31)]  # 30 rows, only 15 distinct firms/domains
contacts += [mk(31, "boss@firm1.co.uk"), mk(32, "ops@colleague.co.uk"), mk(33, "jo@subscriber.co.uk")]
for i, c in enumerate(contacts):
    c["company_number"] = f"{i + 1:08d}"; c["created_at"] = "2026-11-10T00:00:00"
sim.outreach_add(contacts)
sim.insert_subscriber({"id": "ss", "email": "boss@subscriber.co.uk", "pack": "cleaning", "regions": "", "plan": "starter",
                       "status": "active", "unsub_token": "q", "created_at": "now"})
os.environ.update(OUTREACH_ENABLED="true", OUTREACH_APPROVED_VERSION=outreach.TEMPLATE_VERSION, COMPANY_LINE="X Ltd",
                  POSTAL_ADDRESS="1 Road", OUTREACH_MAILBOX="o@bc.co.uk", OUTREACH_SENDER_NAME="Oscar",
                  MS_TENANT_ID="t", MS_CLIENT_ID="c", MS_CLIENT_SECRET="s", OUTREACH_DAILY_LIMIT="4")
sends = []
outreach.graph_token = lambda: "tok"
outreach.graph_send = lambda tok, m: sends.append((m["To"], m["Subject"], day))
day = dt.date(2026, 11, 16)
for _ in range(56):
    for _run in range(3):                                      # someone presses "Run workflow" twice more
        outreach.send(sim, today=day, pause_between=(0, 0))
    if day == dt.date(2026, 12, 7):                            # 12-month clean-up deletes the contact rows...
        sim.outreach_delete_older_than("2099-01-01")
        sim.outreach_add([dict(c, id=c["id"] + "b", optout_token=c["optout_token"] + "b") for c in contacts])  # ...and they win again
        outreach.check.__globals__["companies_house"] = lambda n: {"type": "ltd", "company_status": "active"}
        os.environ["COMPANIES_HOUSE_API_KEY"] = "k"
        outreach.time.sleep = lambda x: None
        for c in sim.outreach_rows(["checked"]):
            sim.outreach_update(c["id"], {"status": "new"})
        outreach.check(sim)
    day += dt.timedelta(days=1)
per_addr, per_domain = {}, {}
for to, subj, _d in sends:
    per_addr[to] = per_addr.get(to, 0) + 1
    per_domain[outreach.domain(to)] = per_domain.get(outreach.domain(to), 0) + 1
assert max(per_addr.values()) == 2 and max(per_domain.values()) == 2, (per_addr, per_domain)   # first email + one follow-up, ever
assert len(per_domain) == 16, per_domain                                   # 15 firms + colleague.co.uk; subscriber's domain never
assert "subscriber.co.uk" not in per_domain
by_day = {}
for *_x, d in sends:
    by_day[d] = by_day.get(d, 0) + 1
assert max(by_day.values()) <= 4, by_day                                   # daily cap holds across 3 runs a day
assert len(sends) == 32 and sum(1 for s_ in sends if s_[1].startswith("Following up")) == 16, len(sends)
assert sim.outreach_rows(["skipped"]) and all("already contacted" in c["skip_reason"] for c in sim.outreach_rows(["skipped"])
                                              if c["id"].endswith("b") and c["email"] not in ("jo@subscriber.co.uk",))
# A reply cancels the follow-up and blocks the domain.
r2 = engine.SQLiteStore(tempfile.mktemp(suffix=".db")); r2.upsert_notices([engine.parse_release(rel("ocds-long", "Long cleaning", "90919200", "UKD3", 5, far))])
r2.outreach_add([mk(1, "a@reply.co.uk", "emailed", emailed_at="2026-11-02T09:00:00+00:00", company_number="11111111"),
                 mk(2, "b@reply.co.uk", company_number="22222222")])
r2.outreach_update("o1", {"status": "replied", "responded_at": "2026-11-03"}); r2.suppress(outreach.keys_for(r2.outreach_rows()[0]), "replied")
assert outreach.plan_batch(r2, dt.date(2026, 11, 16)) == []
print("all tests passed")
