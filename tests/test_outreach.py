import pytest

from amorehiremepls.ai import AIRunner, FakeProvider, Pricing
from amorehiremepls.outreach import OutreachService, RecipientStore, template_connect_note, validate_connect_note
from amorehiremepls.profile import RecipientExclusion, Provenance, build_profile_from_skills
from amorehiremepls.skills import catalog
from tests.test_profile import FAKE_FACTS, FAKE_SALARY

RULES = {"connect_note_max_chars": 300, "no_dashes": True, "no_layoff_mention": True, "mention_applied_role": True, "ask_other_permanent_backend_roles": True}


def test_connect_note_rules_enforced_in_code():
    good = template_connect_note("Sam", "Acme", "Backend Developer")
    v = validate_connect_note(good, "Backend Developer", RULES)
    assert v.ok and v.length < 300
    bad = "Hi Sam, after my recent lay-off I applied to Acme as a Backend Developer. Any other roles? [NAME]"
    v = validate_connect_note(bad, "Backend Developer", RULES)
    assert not v.ok and any("dash" in p for p in v.problems) and any("layoff" in p for p in v.problems) and any("placeholder" in p for p in v.problems)
    claim = validate_connect_note("Hi Sam, I applied for the Backend Developer role. Do you have other permanent backend roles that may fit?", None, RULES)
    assert any("not confirmed" in p for p in claim.problems)
    long = validate_connect_note("x" * 300 + " permanent backend ?", "x", RULES)
    assert any("characters" in p for p in long.problems)


@pytest.mark.mongo
def test_aliases_prior_conversations_unknown_history_and_pending_actions_block_first_contact(store):
    profile = build_profile_from_skills(FAKE_FACTS, FAKE_SALARY)
    rs = RecipientStore(store, profile)
    a = rs.upsert(name="Sam Recruiter", employer="Acme", profile_url="https://www.linkedin.com/in/sam-recruiter/?trk=x", remit="Talent Acquisition, Engineering")
    b = rs.upsert(name="S. Recruiter", employer="Acme Inc", profile_url="https://linkedin.com/in/sam-recruiter", email="sam@acme.example")
    assert a["_id"] == b["_id"] and set(b["aliases"]) == {"li:sam-recruiter", "email:sam@acme.example"}
    # unknown history blocks
    h = rs.history_check(b)
    assert not h["first_contact_allowed"] and any("not yet reconciled" in r for r in h["reasons"])
    rs.mark_reconciled(b["_id"], "checked LinkedIn inbox: no prior messages")
    assert rs.history_check(rs.coll.find_one({"_id": b["_id"]}))["first_contact_allowed"]
    # imported prior conversation via email alias blocks
    rs.import_history(alias="sam@acme.example", kind="message", when="2026-09-01", note="exported LinkedIn thread", source="linkedin_export")
    h = rs.history_check(rs.coll.find_one({"_id": b["_id"]}))
    assert not h["first_contact_allowed"] and "prior conversation exists" in h["reasons"]
    # name-only identity is never enough
    c = rs.upsert(name="Sam Recruiter", employer="Acme")
    assert c["_id"] != a["_id"] and c["identity_confidence"] == "name_only"
    assert any("identity not verified" in r for r in rs.history_check(c)["reasons"])
    # pending action on another verified recipient blocks a second first contact
    d = rs.upsert(name="Dana Hire", employer="Acme", profile_url="https://linkedin.com/in/dana-hire")
    rs.mark_reconciled(d["_id"])
    store.c("actions").insert_one({"_id": "act_x", "kind": "connection_request", "target_id": d["_id"], "status": "ready", "idempotency_key": "cr:d"})
    assert "a pending action already targets this recipient" in rs.history_check(rs.coll.find_one({"_id": d["_id"]}))["reasons"]


@pytest.mark.mongo
def test_identity_exclusions_resolved_only_and_employer_exclusions(store):
    profile = build_profile_from_skills(FAKE_FACTS, FAKE_SALARY)
    rs = RecipientStore(store, profile)
    jess_unknown = rs.upsert(name="Jessica Someone", employer="Acme", profile_url="https://linkedin.com/in/jessica-someone")
    rs.mark_reconciled(jess_unknown["_id"])
    h = rs.history_check(rs.coll.find_one({"_id": jess_unknown["_id"]}))
    assert not h["first_contact_allowed"] and any("unresolved identity exclusion" in r for r in h["reasons"])  # reconcile, do not guess
    profile.preferences.recipient_exclusions = [RecipientExclusion(label="Jessica", resolution="resolved", profile_url="https://linkedin.com/in/jessica-other", provenance=Provenance(source="user"))]
    rs2 = RecipientStore(store, profile)
    assert rs2.history_check(rs.coll.find_one({"_id": jess_unknown["_id"]}))["first_contact_allowed"]  # different identity, same first name
    other = rs2.upsert(name="J Other", employer="Acme", profile_url="https://www.linkedin.com/in/jessica-other/")
    assert rs2.exclusion_for(other) == "recipient excluded: Jessica"
    rbc = rs2.upsert(name="R Person", employer="Royal Bank of Canada", profile_url="https://linkedin.com/in/r-person")
    assert "RBC" in rs2.exclusion_for(rbc)


@pytest.mark.mongo
def test_draft_rejects_bad_model_output_and_send_is_handoff_or_scoped(store):
    profile = build_profile_from_skills(FAKE_FACTS, FAKE_SALARY)
    provider = FakeProvider({"OutreachOutput": lambda u: {"text": "Hi Sam, I was laid-off recently. Any roles?"}})
    svc = OutreachService(store, profile, AIRunner(store, provider, catalog, Pricing(None)))
    rec = svc.recipients.upsert(name="Sam Recruiter", employer="Acme", profile_url="https://linkedin.com/in/sam-recruiter")
    svc.recipients.mark_reconciled(rec["_id"])
    d = svc.draft(rec["_id"], "connection_request", applied_role="Backend Developer")
    assert d["method"] == "template_after_rejection" and d["validation"]["ok"] and "Backend Developer" in d["text"]
    res = svc.send(d["_id"], "w1", channel="linkedin")
    assert res["status"] == "handoff" and res["text"] == d["text"]
    assert store.c("outreach").find_one({"_id": d["_id"]})["status"] == "handoff"
    # email without verified address never invents one
    assert svc.send(d["_id"], "w1", channel="email")["status"] == "blocked"
    # email configured but no scope -> denied by authorization, nothing sent
    svc2 = OutreachService(store, profile, None, smtp={"host": "smtp.example", "from": "joss@example.com"})
    rec2 = svc2.recipients.upsert(name="Dana", employer="Acme", email="dana@acme.example", profile_url="https://linkedin.com/in/dana")
    svc2.recipients.mark_reconciled(rec2["_id"])
    d2 = svc2.draft(rec2["_id"], "outreach_first_contact", applied_role="Backend Developer")
    out = svc2.send(d2["_id"], "w1", channel="email")
    assert out["status"] == "denied"
