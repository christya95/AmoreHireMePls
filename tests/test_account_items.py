"""Logins the app saves in 1Password for accounts it creates, the scope that allows it, and the settings.

The password manager here is the in-memory fake (`tests/fixtures/fake_op.py`). Every employer, site, address and
password is fictional. The real tool is never run."""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta

import pytest

from amorehiremepls.candidate.profile import Fact, Profile, Provenance
from amorehiremepls.core.account_items import (
    APP_TAG,
    CONFIRMED_TAG,
    F_PASSWORDS,
    F_RECORD,
    F_STATUS,
    KIND_TAG,
    SECTION,
    UNCONFIRMED_TAG,
    ItemError,
    NotOurItem,
    OpCliWriter,
    PasswordLimit,
    PasswordRules,
    RulesImpossible,
    account_email,
    answer_account_ask,
    existing_logins,
    fits,
    item_spec,
    item_template,
    item_title,
    made_by_app,
    mark_confirmed,
    mark_not_created,
    recipe_for,
)
from amorehiremepls.core.actions import ACTION_SCOPE, ActionStore
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding, material_hash, site_account_key
from amorehiremepls.core.config import Settings
from amorehiremepls.core.credentials import NOT_CREATED_TAG, ApprovalNotGiven, NoSavedLogin, OpCliSource, SourceUnavailable, choose_login
from amorehiremepls.core.sealed import HandoffKey, sign_public
from amorehiremepls.core.site_signin import ask_context
from tests.fixtures.fake_op import VAULT, FakeOp, generated

SECRET = "s" * 40
EMAIL = "avery.fictional@example.test"
JOB = {"employer": "Northwind Traders Inc", "title": "Account Executive", "requisition_id": "R-1042", "posting_url": "https://careers.northwindtraders.test/jobs/1042"}
SIGNIN = "https://signin.northwindtraders.test/login"
RECORD = "account:owner:northwindtraders.test:0123456789abcdef"


def _spec(**kw):
    return item_spec(JOB, kw.pop("signin_url", SIGNIN), EMAIL, kw.pop("record", RECORD), vault=VAULT["name"], today="2026-11-13", **kw)


def _tool(**kw) -> tuple[FakeOp, OpCliSource, OpCliWriter]:
    fake = FakeOp(**kw)
    source = OpCliSource(binary="python3", run=fake)
    return fake, source, OpCliWriter(OpCliSource(binary="python3", vault=VAULT["name"], run=fake))


def _ask(step: str = "account", host: str = "signup.northwindtraders.test") -> tuple[HandoffKey, dict]:
    key = HandoffKey()
    ask = {"ask_id": "ask_fixture_1", "host": host, "step": step, "key": key.public}
    ask["key_sig"] = sign_public(SECRET, key.public, ask_context(ask))
    return key, ask


def _argv(fake: FakeOp) -> str:
    return json.dumps([args for args, _ in fake.calls])


# -- what the owner sees in 1Password ------------------------------------------------------------------------
def test_the_title_tells_an_employers_own_site_from_a_shared_job_site():
    assert item_title("Northwind Traders Inc", "careers.northwindtraders.test") == "Northwind Traders Inc job application account (careers.northwindtraders.test)"
    # a site many employers share: named for the site, never for the employer of the job that happened to need it
    assert item_title("Northwind Traders Inc", "apply.open-roles.test") == "Open Roles job site account (apply.open-roles.test)"
    assert item_title("Northwind Traders Inc", "apply.open-roles.test", site_name="Open Roles Board") == "Open Roles Board job site account (apply.open-roles.test)"
    # an adapter may say which form applies
    assert item_title("Northwind Traders Inc", "northwind.hiring-desk.test", shared=False) == "Northwind Traders Inc job application account (northwind.hiring-desk.test)"
    assert item_title("", "careers.northwindtraders.test") == "Northwindtraders job site account (careers.northwindtraders.test)"


def test_the_template_carries_the_website_the_email_the_tags_and_the_section_and_no_password():
    spec = _spec(careers_url="https://careers.northwindtraders.test/")
    template = item_template(spec)
    assert template["category"] == "LOGIN" and template["tags"] == [APP_TAG, KIND_TAG, UNCONFIRMED_TAG]
    assert template["urls"][0] == {"label": "website", "primary": True, "href": SIGNIN} and template["urls"][1]["href"] == "https://careers.northwindtraders.test/"
    fields = {row["label"]: row for row in template["fields"]}
    assert fields["username"]["value"] == EMAIL and fields["password"]["value"] == ""
    section = {row["label"]: row["value"] for row in template["fields"] if row.get("section")}
    assert section == {
        "Employer": "Northwind Traders Inc",
        "Job title": "Account Executive",
        "Requisition": "R-1042",
        "Job link": JOB["posting_url"],
        "Created on": "2026-11-13",
        F_STATUS: "Unconfirmed, saved before the form was sent",
        F_RECORD: RECORD,
        F_PASSWORDS: "1",
    }
    assert template["sections"][0]["label"] == SECTION
    assert "Account Executive at Northwind Traders Inc" in fields["notesPlain"]["value"] and "signin.northwindtraders.test" in fields["notesPlain"]["value"]
    assert EMAIL not in repr(spec)
    for bad in ({"signin_url": "http://signin.northwindtraders.test/login"}, {"record": " "}):
        with pytest.raises(ItemError):
            _spec(**bad)


def test_the_account_email_is_the_profile_email_unless_the_setting_overrides_it():
    profile = Profile(facts=[Fact(category="contact", key="email", value=EMAIL, provenance=Provenance(source="test"))])
    assert account_email(Settings(_env_file=None), profile) == profile.fact_value("email")
    assert account_email(Settings(_env_file=None, site_account_email=" other.address@example.test "), profile) == "other.address@example.test"


# -- password rules ------------------------------------------------------------------------------------------
def test_the_recipe_follows_the_pages_rules_and_impossible_rules_are_not_guessed():
    assert recipe_for(None) == "letters,digits,symbols,32"
    assert recipe_for(PasswordRules(max_length=16)) == "letters,digits,symbols,16"
    assert recipe_for(PasswordRules(min_length=40)) == "letters,digits,symbols,40"
    assert recipe_for(PasswordRules(forbidden="@")) == "letters,digits,32"  # one of the generator's symbols is forbidden: none are used
    assert recipe_for(PasswordRules(allowed_symbols="#$%")) == "letters,digits,32"
    assert recipe_for(PasswordRules(allowed_symbols="!@.-_*#")) == "letters,digits,symbols,32"
    assert recipe_for(PasswordRules(no_symbols=True, max_length=12)) == "letters,digits,12"
    for rules in (
        PasswordRules(need_symbol=True, allowed_symbols="#$"),
        PasswordRules(max_length=6),
        PasswordRules(min_length=80),
        PasswordRules(need_symbol=True, no_symbols=True),
    ):
        with pytest.raises(RulesImpossible):
            recipe_for(rules)


def test_a_generated_password_is_checked_against_the_stated_rules():
    assert fits(None, "abcDEF12!") and not fits(None, "")
    assert not fits(PasswordRules(min_length=10), "abcDEF12!") and not fits(PasswordRules(max_length=8), "abcDEF12!")
    assert not fits(PasswordRules(forbidden="!"), "abcDEF12!") and not fits(PasswordRules(no_symbols=True), "abcDEF12!")
    assert not fits(PasswordRules(allowed_symbols="#"), "abcDEF12!") and fits(PasswordRules(allowed_symbols="!#"), "abcDEF12!")
    assert not fits(PasswordRules(need_digit=True), "abcDEF!") and not fits(PasswordRules(need_upper=True), "abc12!")
    assert not fits(PasswordRules(need_lower=True), "ABC12!") and not fits(PasswordRules(need_symbol=True), "abcDEF12")
    assert fits(PasswordRules(min_length=8, max_length=20, need_lower=True, need_upper=True, need_digit=True, need_symbol=True), "abcDEF12!")


# -- writing through the tool --------------------------------------------------------------------------------
def test_create_saves_one_login_with_a_generated_password_and_no_secret_on_the_command_line():
    fake, _, writer = _tool()
    made = writer.create(_spec(), "letters,digits,symbols,32")
    args, piped = fake.calls[-1]
    assert args[1:4] == ["item", "create", "-"] and "--generate-password=letters,digits,symbols,32" in args and args[-2:] == ["--vault", VAULT["name"]]
    assert made.password == generated("letters,digits,symbols,32", 1) and len(made.password) == 32
    # the password is made inside the tool: it is in no argument and not in what was piped; the email is piped, never an argument
    assert made.password not in _argv(fake) and made.password not in piped and EMAIL not in _argv(fake) and EMAIL in piped
    assert made.password not in repr(made) and EMAIL not in repr(made)
    item = fake.items[made.login.ref]
    assert item["title"] == "Northwind Traders Inc job application account (signin.northwindtraders.test)"
    assert set(item["tags"]) == {APP_TAG, KIND_TAG, UNCONFIRMED_TAG} and made.login.hosts == ("signin.northwindtraders.test",)
    assert made_by_app(made.login) and writer.owns(made.login, RECORD) and not writer.owns(made.login, "account:owner:other.test:ffff")


def test_a_tool_that_answers_without_the_password_is_read_once_more():
    fake, _, writer = _tool(echo_password=False)
    made = writer.create(_spec(), "letters,digits,20")
    assert made.password == fake.password_of(made.login.ref) and fake.verbs()[-2:] == ["item create", "item get"]


def test_a_new_password_goes_on_the_same_item_three_passwords_in_all():
    fake, _, writer = _tool()
    first = writer.create(_spec(), "letters,digits,symbols,32")
    second = writer.regenerate(first.login, RECORD, "letters,digits,16", made=1)
    third = writer.regenerate(second.login, RECORD, "letters,digits,16", made=2)
    assert len(fake.items) == 1 and first.login.ref == second.login.ref == third.login.ref
    assert len({first.password, second.password, third.password}) == 3 and len(third.password) == 16
    assert second.login.fingerprint != first.login.fingerprint  # the saved login changed, so a remembered refusal no longer blocks it
    assert fake.field(first.login.ref, F_PASSWORDS) == "3" and fake.field(first.login.ref, "Password rules used") == "letters,digits,16"
    for secret in (first.password, second.password, third.password):
        assert secret not in _argv(fake)
    with pytest.raises(PasswordLimit):
        writer.regenerate(third.login, RECORD, "letters,digits,16", made=3)
    assert fake.password_of(first.login.ref) == third.password


def test_confirming_swaps_the_state_tag_and_keeps_the_owners_own_tags():
    fake, _, writer = _tool()
    made = writer.create(_spec(), "letters,digits,symbols,32")
    fake.items[made.login.ref]["tags"].append("job-search")
    done = mark_confirmed(writer, made.login, RECORD, day="2026-11-14")
    assert set(fake.items[done.ref]["tags"]) == {APP_TAG, KIND_TAG, CONFIRMED_TAG, "job-search"} and CONFIRMED_TAG in done.tags
    assert fake.field(done.ref, F_STATUS) == "Confirmed on 2026-11-14" and fake.password_of(done.ref) == made.password
    refused = mark_not_created(writer, done, RECORD)
    assert NOT_CREATED_TAG in refused.tags and CONFIRMED_TAG not in refused.tags and fake.field(done.ref, F_STATUS) == "Not created, the site refused"
    with pytest.raises(ItemError):
        writer.annotate(done, RECORD, "some-other-tag", "x")


def test_only_items_with_the_apps_own_marks_are_ever_renamed_annotated_or_archived():
    fake, source, writer = _tool()
    fake.add_login("owner-item-1", "My own login", ("careers.northwindtraders.test",), username=EMAIL)
    # carries both tags but not the record this caller holds: still not the app's to touch
    section = {"id": "s", "label": SECTION}
    fake.add_login(
        "lookalike-1", "Looks like ours", ("other.test",), tags=(APP_TAG, KIND_TAG), extra=[{"id": "r", "section": section, "label": F_RECORD, "value": "another-record"}]
    )
    before = json.dumps(fake.items, sort_keys=True)
    for login in source.logins():
        for act in (
            lambda item: writer.rename(item, RECORD, "New name"),
            lambda item: writer.archive(item, RECORD),
            lambda item: mark_confirmed(writer, item, RECORD),
            lambda item: writer.regenerate(item, RECORD, "letters,digits,16", made=1),
            lambda item: writer.archive(item, ""),
        ):
            with pytest.raises(NotOurItem):
                act(login)
    assert json.dumps(fake.items, sort_keys=True) == before and not fake.archived
    assert not any(verb in ("item edit", "item delete") for verb in fake.verbs())
    made = writer.create(_spec(), "letters,digits,symbols,32")
    assert writer.rename(made.login, RECORD, "Northwind Traders Inc job application account (old)").title.endswith("(old)")
    writer.archive(made.login, RECORD)
    assert fake.archived == [made.login.ref] and not fake.deleted and "--archive" in fake.calls[-1][0]


def test_tool_errors_become_categories_and_a_missing_vault_is_named():
    fake, _, writer = _tool()
    spec = item_spec(JOB, SIGNIN, EMAIL, RECORD, vault="No Such Vault")
    with pytest.raises(SourceUnavailable) as caught:
        writer.create(spec, "letters,digits,symbols,32")
    assert caught.value.kind == "failed" and caught.value.detail == "vault_missing" and "No Such Vault" not in str(caught.value)
    fake.fail = ['[ERROR] invalid argument "letters,65" for "--generate-password" flag: Length must be between 1 and 64', subprocess.TimeoutExpired("op", 1)]
    with pytest.raises(SourceUnavailable) as caught:
        writer.create(_spec(), "letters,65")
    assert caught.value.detail == "recipe"
    with pytest.raises(ApprovalNotGiven):
        writer.create(_spec(), "letters,digits,symbols,32")
    assert not fake.items


def test_a_login_marked_not_created_is_never_offered_for_sign_in():
    fake, source, writer = _tool()
    made = writer.create(_spec(), "letters,digits,symbols,32")
    assert choose_login(source.logins(), "signin.northwindtraders.test").ref == made.login.ref
    mark_not_created(writer, made.login, RECORD)
    with pytest.raises(NoSavedLogin):
        choose_login(source.logins(), "signin.northwindtraders.test")
    # it still counts as saved for the site, so a second item is never made beside it
    assert [login.ref for login in existing_logins(source, "careers.northwindtraders.test")] == [made.login.ref]


# -- the press on the dashboard ------------------------------------------------------------------------------
def test_the_press_saves_the_login_before_anything_is_sent_and_hands_it_over_sealed():
    fake, source, writer = _tool()
    key, ask = _ask()
    answer = answer_account_ask(ask, writer, source, SECRET, _spec(), PasswordRules(max_length=16, need_digit=True))
    assert answer.ok and answer.flash == "account_saved" and answer.made == 1 and answer.title.startswith("Northwind Traders Inc job application account")
    ref = next(iter(fake.items))
    password = fake.password_of(ref)
    assert len(password) == 16 and password not in json.dumps(answer.reply) and password not in repr(answer) and EMAIL not in json.dumps(answer.reply)
    opened = key.open(answer.reply["box"], ask_context(ask))
    assert opened["password"] == password and opened["username"] == EMAIL and opened["host"] == "signup.northwindtraders.test" and opened["title"] == answer.title
    assert UNCONFIRMED_TAG in fake.items[ref]["tags"] and fake.verbs() == ["item list", "item create"]
    # a box for this ask is no use to another task
    with pytest.raises(Exception, match="could not open"):
        HandoffKey().open(answer.reply["box"], ask_context(ask))


def test_no_account_is_created_while_any_login_for_the_site_exists():
    fake, source, writer = _tool()
    fake.add_login("owner-item-1", "Old careers login", ("jobs.northwindtraders.test",), username="someone.else@example.test")
    key, ask = _ask()
    answer = answer_account_ask(ask, writer, source, SECRET, _spec())
    assert not answer.ok and answer.reply == {"kind": "error", "error": "login_exists"} and len(fake.items) == 1 and "item create" not in fake.verbs()
    # a login for another site does not block
    fake2, source2, writer2 = _tool()
    fake2.add_login("owner-item-2", "Another site", ("careers.other-employer.test",))
    assert answer_account_ask(ask, writer2, source2, SECRET, _spec()).ok


def test_a_press_whose_answer_was_lost_reuses_the_saved_login_and_never_makes_a_second():
    fake, source, writer = _tool()
    key, ask = _ask()
    assert answer_account_ask(ask, writer, source, SECRET, _spec()).ok
    key2, ask2 = _ask()
    again = answer_account_ask(ask2, writer, source, SECRET, _spec())
    assert again.ok and len(fake.items) == 1 and fake.verbs().count("item create") == 1
    assert key2.open(again.reply["box"], ask_context(ask2))["password"] == fake.password_of(next(iter(fake.items)))
    # the same half-made item under another record is not adopted
    other = answer_account_ask(_ask()[1], writer, source, SECRET, _spec(record="account:owner:northwindtraders.test:ffffffffffffffff"))
    assert other.reply == {"kind": "error", "error": "login_exists"} and len(fake.items) == 1


def test_a_password_that_does_not_fit_is_replaced_on_the_same_item_up_to_the_ceiling():
    rules = PasswordRules(need_digit=True)
    fake, source, writer = _tool(passwords=["nodigitsHERE!!", "stillNoDigits!", "fits1Now!ok"])
    key, ask = _ask()
    answer = answer_account_ask(ask, writer, source, SECRET, _spec(), rules)
    assert answer.ok and answer.made == 3 and len(fake.items) == 1 and key.open(answer.reply["box"], ask_context(ask))["password"] == "fits1Now!ok"
    fake, source, writer = _tool(passwords=["nodigitsHERE!!", "stillNoDigits!", "againNoDigits!", "fits1Now!ok"])
    stopped = answer_account_ask(_ask()[1], writer, source, SECRET, _spec(), rules)
    assert not stopped.ok and stopped.reply["error"] == "password_rules" and stopped.made == 3 and len(fake.items) == 1 and fake.verbs().count("item edit") == 2
    # rules the generator cannot meet: nothing is saved and nothing is made up
    fake, source, writer = _tool()
    impossible = answer_account_ask(_ask()[1], writer, source, SECRET, _spec(), PasswordRules(need_symbol=True, allowed_symbols="#"))
    assert impossible.reply["error"] == "password_rules" and not fake.items


def test_make_a_new_password_is_its_own_press_on_the_same_item():
    fake, source, writer = _tool()
    first = answer_account_ask(_ask()[1], writer, source, SECRET, _spec())
    login = source.logins()[0]
    old = fake.password_of(login.ref)
    key, ask = _ask("password")
    second = answer_account_ask(ask, writer, source, SECRET, _spec(), PasswordRules(max_length=12), login=login, made=first.made)
    assert second.ok and second.flash == "account_password_made" and second.made == 2 and len(fake.items) == 1
    new = key.open(second.reply["box"], ask_context(ask))["password"]
    assert new != old and len(new) == 12 and new == fake.password_of(login.ref)
    assert answer_account_ask(_ask("password")[1], writer, source, SECRET, _spec(), login=source.logins()[0], made=3).reply["error"] == "password_limit"
    assert answer_account_ask(_ask("password")[1], writer, source, SECRET, _spec(), made=1).reply["error"] == "mismatch"


def test_a_missed_window_keeps_the_ask_open_and_other_failures_say_what_to_do():
    fake, source, writer = _tool()
    key, ask = _ask()
    fake.fail = ["[ERROR] authorization prompt dismissed, please try again"]
    missed = answer_account_ask(ask, writer, source, SECRET, _spec())
    assert missed.missed and missed.reply is None and missed.flash == "account_not_approved" and not fake.items
    fake.fail = ["[ERROR] dial tcp: connection refused"] * 3
    passing = answer_account_ask(ask, writer, source, SECRET, _spec(), sleep=lambda seconds: None)
    assert passing.missed and passing.flash == "account_save_failed" and not fake.items
    spec = item_spec(JOB, SIGNIN, EMAIL, RECORD, vault="No Such Vault")
    assert answer_account_ask(ask, writer, source, SECRET, spec).flash == "account_vault_missing"
    # an ask from another app secret, for another site, or with an unknown step is not answered at all
    assert answer_account_ask(ask, writer, source, "t" * 40, _spec()).reply["error"] == "mismatch"
    assert answer_account_ask(_ask(host="signup.other-employer.test")[1], writer, source, SECRET, _spec()).reply["error"] == "mismatch"
    assert answer_account_ask(_ask("login")[1], writer, source, SECRET, _spec()).reply["error"] == "mismatch"
    here = OpCliWriter(OpCliSource(binary="no-such-tool-here"))
    assert answer_account_ask(ask, here, source, SECRET, _spec()).flash == "account_not_here"
    assert not fake.items


# -- the scope -----------------------------------------------------------------------------------------------
def test_the_account_key_is_person_site_and_email_and_holds_no_address():
    key = site_account_key("user_1", "https://signup.northwindtraders.test/register", EMAIL)
    assert key.startswith("account:user_1:northwindtraders.test:") and len(key.rsplit(":", 1)[1]) == 16 and "avery" not in key
    assert key == site_account_key("user_1", "careers.northwindtraders.test", EMAIL.upper() + " ")  # one account per site, whatever the host
    assert key != site_account_key("user_2", "careers.northwindtraders.test", EMAIL) and key != site_account_key("user_1", "careers.northwindtraders.test", "other@example.test")
    assert site_account_key(None, "careers.northwindtraders.test", EMAIL).startswith("account:owner:northwindtraders.test:")
    for bad in (("u", "", EMAIL), ("u", "careers.northwindtraders.test", "not-an-address")):
        with pytest.raises(ValueError):
            site_account_key(*bad)
    assert ACTION_SCOPE["site_account_create"] == "site_account_create"


@pytest.mark.mongo
def test_an_approval_to_apply_never_allows_creating_an_account(store):
    auth, acts = AuthorizationStore(store), ActionStore(store)
    key = site_account_key("user_1", "signup.northwindtraders.test", EMAIL)
    offer = material_hash(extra={"host": "signup.northwindtraders.test", "email": EMAIL, "boxes": ["terms"]})
    # an approval to apply, even for the same target and the same material, is a different kind
    auth.grant("application_submit", ScopeBinding(target_id=key, material_hash=offer))
    action = acts.prepare("site_account_create", key, key, {"host": "signup.northwindtraders.test"}, offer, host="signup.northwindtraders.test")
    calls: list[int] = []
    assert acts.execute(action["_id"], "w1", lambda doc: calls.append(1) or {"verified": True}).status == "denied" and not calls
    with pytest.raises(ValueError):
        auth.grant("site_account_create", ScopeBinding(job_filters={"employers": ["Northwind Traders Inc"]}), mode="batch")  # one approval per site, never a batch
    # its own scope: exact target, exact offer, the sign-up host, one use
    scope = auth.grant("site_account_create", ScopeBinding(target_id=key, material_hash=offer, allowed_hosts=["signup.northwindtraders.test"]), ttl=timedelta(minutes=30))
    changed = material_hash(extra={"host": "signup.northwindtraders.test", "email": EMAIL, "boxes": ["terms", "marketing"]})
    assert not auth.check("site_account_create", key, changed, host="signup.northwindtraders.test").allowed
    assert not auth.check("site_account_create", key, offer, host="elsewhere.test").allowed
    assert not auth.check("application_submit", "job_1", offer).allowed
    done = acts.execute(action["_id"], "w1", lambda doc: calls.append(1) or {"verified": True})
    assert done.status == "succeeded" and calls == [1] and auth.get(scope.id).status == "consumed"
    # replay, a restart or a second worker: the same key gives the saved outcome and the form is not sent again
    again = acts.prepare("site_account_create", key, key, {"host": "signup.northwindtraders.test"}, offer)
    assert again["_id"] == action["_id"] and acts.execute(again["_id"], "w2", lambda doc: calls.append(1) or {"verified": True}).status == "succeeded" and calls == [1]


# -- settings ------------------------------------------------------------------------------------------------
def test_the_new_settings_are_off_or_empty_by_default(monkeypatch):
    for name in ("AMORE_SITE_ACCOUNT_CREATE", "AMORE_SITE_ACCOUNT_VAULT", "AMORE_SITE_ACCOUNT_EMAIL", "AMORE_MAIL_VERIFICATION", "AMORE_MAIL_GRANT_STORE"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    assert s.site_account_create is False and s.mail_verification is False and s.site_account_vault == "" and s.site_account_email == ""
    assert s.site_account_password_tries == 3 and s.mail_wait_seconds == 300 and s.mail_grant_store == "1password"
    monkeypatch.setenv("AMORE_SITE_ACCOUNT_VAULT", "Fixture Vault")
    monkeypatch.setenv("AMORE_SITE_ACCOUNT_CREATE", "true")
    on = Settings(_env_file=None)
    assert on.site_account_create and on.site_account_vault == "Fixture Vault"


@pytest.mark.mongo
def test_creating_accounts_needs_sign_in_and_both_switches_default_off(store, tmp_dirs):
    from amorehiremepls.core.runtime import Runtime

    rt = Runtime(Settings(_env_file=None, private_dir=tmp_dirs["private"], artifact_dir=tmp_dirs["artifacts"], log_dir=tmp_dirs["logs"]), store=store)
    assert not rt.site_account_create_enabled() and not rt.mail_verification_enabled()
    rt.store.set_control("site_account_create", True)
    assert not rt.site_account_create_enabled()  # sign-in with saved logins is still off
    rt.store.set_control("site_signin", True)
    rt.store.set_control("mail_verification", True)
    assert rt.site_account_create_enabled() and rt.mail_verification_enabled()
    writer = rt.item_writer()
    assert isinstance(writer, OpCliWriter) and writer.tries == 3
