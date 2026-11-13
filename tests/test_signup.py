"""Recognising a create-account page and planning what would be typed and ticked. Nothing here acts on a page.

Page descriptions and HTML come from `tests/fixtures/signup_pages.py`. Every site, person and address is fictional."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from amorehiremepls.apply.signup import (
    PAGE_JS,
    SIGNUP_MARK,
    SignUpPlan,
    box_kind,
    field_role,
    merge_rules,
    named_senders,
    page_lines,
    page_rules,
    plan_signup,
    profile_values,
    read_page,
    rules_from_text,
    signup_control,
)
from amorehiremepls.candidate.profile import Fact, Profile, Provenance
from amorehiremepls.core.account_items import PasswordRules
from tests.fixtures.signup_pages import (
    CODE_HTML,
    PRIVACY,
    REJECTED_HTML,
    SIGNUP_HTML,
    SITE,
    TERMS,
    URL,
    WIDGET_HTML,
    button,
    code_page,
    consent_box,
    field,
    link,
    page,
    signin_page,
    signup_fields,
    signup_page,
)

EMAIL = "avery.fictional@example.test"
AVERY = {"first_name": "Avery", "last_name": "Fictional", "full_name": "Avery Fictional", "phone": "555 0100"}  # the fictional developer
MORGAN = {"first_name": "Morgan", "last_name": "Sample", "full_name": "Morgan Sample", "preferred_name": "Mo"}  # the fictional account executive
SIGN_UP_RULES = PasswordRules(min_length=10, need_lower=True, need_upper=True, need_digit=True)


def _plan(description: dict, known: dict | None = None, **kw) -> SignUpPlan:
    return plan_signup(description, AVERY if known is None else known, kw.pop("email", EMAIL), **kw)


def _sent(description: dict, **kw):
    """Read a page as it is after the form was sent: only what the create-account page did not already say counts."""
    return read_page(description, before=page_lines(signup_page()), account_email=EMAIL, **kw)


# -- the form and its plan -----------------------------------------------------------------------------------
def test_a_create_account_form_is_recognised_and_planned_without_a_password_in_it():
    description = signup_page()
    seen = read_page(description)
    assert seen.kind == "signup_form" and seen.host == SITE and seen.stop == "" and seen.rules == SIGN_UP_RULES
    plan = _plan(description)
    assert plan.state == "ready" and plan.stop == "" and not plan.questions and plan.recipe == "letters,digits,symbols,32"
    assert [(item.key, item.role, item.source, item.value) for item in plan.typed] == [
        ("k1", "first_name", "profile", "Avery"),
        ("k2", "last_name", "profile", "Fictional"),
        ("k3", "email", "account_email", EMAIL),
        ("k4", "password", "password_manager", ""),
        ("k5", "password_confirm", "password_manager", ""),
    ]
    assert [(box.key, box.kind, box.tick) for box in plan.boxes] == [("k7", "consent", True), ("k8", "marketing", False), ("k9", "remember", False)]
    assert plan.left_blank == ["Mobile number (optional)"] and (plan.submit, plan.submit_text) == ("k10", "Create account")
    assert plan.terms == [("Terms of Use", TERMS), ("Privacy Policy", PRIVACY)]
    assert plan.typed[0].label == "First name"  # the star is the page's mark, not part of the name


def test_the_card_names_everything_and_the_plan_shows_no_typed_value_when_printed():
    plan = _plan(signup_page())
    card = plan.card()
    assert [row["value"] for row in card["typed"]] == ["Avery", "Fictional", EMAIL, "", ""] and card["button"] == "Create account"
    assert [row["label"] for row in card["ticked"]] == ["I agree to the Terms of Use and the Privacy Policy"]
    assert [(row["label"], row["kind"]) for row in card["not_ticked"]] == [
        ("Send me job alerts and news about roles like this", "marketing"),
        ("Remember me on this computer", "remember"),
    ]
    assert EMAIL not in repr(plan) and "Avery" not in repr(plan)
    material = json.dumps(plan.material())
    assert EMAIL not in material and "Avery" not in material and "Fictional" not in material


def test_the_approval_is_bound_to_what_would_be_typed_ticked_and_pressed():
    base = _plan(signup_page()).material()
    assert base == _plan(signup_page()).material()
    other_email = _plan(signup_page(), email="someone.else@example.test").material()
    reworded = signup_fields()
    reworded[6] = replace_label(reworded[6], "I agree to the Terms of Use, the Privacy Policy and the Candidate Agreement")
    new_button = signup_page(buttons=[button("k10", "Create account and apply")])
    more = signup_fields() + [field("k20", label="Middle name")]
    for changed in (
        other_email,
        _plan(signup_page(reworded)).material(),
        _plan(new_button).material(),
        _plan(signup_page(more)).material(),
        _plan(signup_page(), MORGAN).material(),
    ):
        assert changed != base


def replace_label(item: dict, label: str) -> dict:
    return {**item, "label": label}


@pytest.mark.parametrize(("known", "first", "last"), [(AVERY, "Avery", "Fictional"), (MORGAN, "Morgan", "Sample")])
def test_the_name_comes_from_the_candidates_own_profile(known, first, last):
    fields = [field("k1", label="Full name", required=True), field("k2", label="Preferred name"), *signup_fields()[2:5], consent_box()]
    plan = _plan(signup_page(fields), known)
    assert plan.state == "ready" and plan.typed[0].value == f"{first} {last}" and plan.typed[0].source == "profile"
    typed = {item.role: item.value for item in plan.typed}
    assert typed.get("preferred_name") == known.get("preferred_name") and ("Preferred name" in plan.left_blank) == ("preferred_name" not in known)


def test_profile_values_use_the_legal_name_and_nothing_is_made_up():
    def fact(key: str, value: str) -> Fact:
        return Fact(category="contact", key=key, value=value, provenance=Provenance(source="test"))

    full = Profile(facts=[fact("legal_first_name", "Avery"), fact("legal_last_name", "Fictional"), fact("preferred_name", "Ave"), fact("phone", "555 0100"), fact("email", EMAIL)])
    assert profile_values(full) == {"first_name": "Avery", "last_name": "Fictional", "full_name": "Avery Fictional", "preferred_name": "Ave", "phone": "555 0100"}
    assert profile_values(Profile(facts=[fact("legal_first_name", "Avery")])) == {"first_name": "Avery"}
    # a name the profile does not have is asked for, never invented, even when the page does not star the box
    plan = _plan(signup_page(), {"first_name": "Avery"})
    assert plan.state == "ask" and [(q.key, q.why) for q in plan.questions] == [("k2", "profile_missing")]
    answered = _plan(signup_page(), {"first_name": "Avery"}, answers={"k2": "Fictional"})
    assert answered.state == "ready" and [(item.source, item.value) for item in answered.typed if item.key == "k2"] == [("owner", "Fictional")]


@pytest.mark.parametrize(
    ("item", "role"),
    [
        (field("a", label="Email address"), "email"),
        (field("a", "email"), "email"),
        (field("a", label="Work email", name="username"), "email"),
        (field("a", label="Username"), "username"),
        (field("a", label="Username or email"), "username"),
        (field("a", name="firstName"), "first_name"),
        (field("a", placeholder="Surname"), "last_name"),
        (field("a", label="Your name"), "full_name"),
        (field("a", label="Company name"), "other"),
        (field("a", label="Mobile"), "phone"),
        (field("a", label="Date of birth"), "birth_date"),
        (field("a", "select", "Country"), "location"),
        (field("a", label="Postal code"), "location"),
        (field("a", label="Referral code"), "other"),
        (field("a", label="Verification code"), "code"),
        (field("a", "select", "Security question"), "security_question"),
        (field("a", label="Answer to your security question"), "security_answer"),
        (field("a", label="What should we call you", autocomplete="nickname"), "preferred_name"),
        (field("a", label="Tell us about yourself"), "other"),
        (field("a", "password", "Choose a password"), "password"),
    ],
)
def test_a_field_is_named_by_what_a_person_sees_beside_it(item, role):
    assert field_role(item) == role


# -- boxes ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("I agree to the Terms of Use and the Privacy Policy", "consent"),
        ("I have read the privacy notice for candidates", "consent"),
        ("Send me job alerts", "marketing"),
        ("Yes, keep me informed about future roles", "marketing"),
        ("Join our talent community", "marketing"),
        ("I agree to the Terms and to receive marketing emails", "bundled"),
        ("I accept the privacy policy and agree my details may be shared with partners", "bundled"),
        ("Keep me signed in", "remember"),
        ("I am 18 or older", "other"),
        ("I agree", "other"),
    ],
)
def test_boxes_are_told_apart_by_their_own_words(label, kind):
    assert box_kind(field("a", "checkbox", label)) == kind


def test_consent_is_ticked_marketing_never_and_anything_else_required_is_asked():
    def boxes(*extra) -> dict:
        return signup_page([*signup_fields()[:5], *extra])

    # agreeing to the terms is ticked even when the page forgot to mark the box as required
    assert [(b.kind, b.tick) for b in _plan(boxes(consent_box(required=False))).boxes] == [("consent", True)]
    # a marketing box is left alone, and one that arrives ticked is to be cleared
    marketing = _plan(boxes(consent_box(), field("m", "checkbox", "Email me news and offers", checked=True)))
    assert marketing.state == "ready" and [(b.key, b.tick) for b in marketing.boxes] == [("k7", True), ("m", False)]
    # one box for the terms and for marketing: optional is left, required is the owner's call
    joined = field("j", "checkbox", "I agree to the Terms and to receive marketing emails")
    assert [(b.kind, b.tick) for b in _plan(boxes(joined)).boxes] == [("bundled", False)] and _plan(boxes(joined)).state == "ready"
    must = _plan(boxes({**joined, "required": True}))
    assert must.state == "ask" and [(q.key, q.why, q.kind) for q in must.questions] == [("j", "bundled_consent", "box")] and not must.boxes[0].tick
    assert _plan(boxes({**joined, "required": True}), answers={"j": "yes"}).boxes[0].tick and _plan(boxes({**joined, "required": True}), answers={"j": "yes"}).state == "ready"
    assert not _plan(boxes({**joined, "required": True}), answers={"j": "no"}).boxes[0].tick
    # a required marketing box and a required box of another kind are asked about too; the owner's yes never ticks an optional one
    for extra, why in ((field("m", "checkbox", "Send me job alerts *"), "bundled_consent"), (field("o", "checkbox", "I am 18 or older", required=True), "required_box")):
        asked = _plan(boxes(consent_box(), extra))
        assert asked.state == "ask" and [q.why for q in asked.questions] == [why] and [b.tick for b in asked.boxes] == [True, False]
    optional = _plan(boxes(consent_box(), field("m", "checkbox", "Send me job alerts")), answers={"m": "yes"})
    assert optional.state == "ready" and [b.tick for b in optional.boxes] == [True, False]


# -- questions only the owner can answer ---------------------------------------------------------------------
def test_a_security_question_a_birth_date_or_a_username_is_asked_never_guessed():
    extra = [
        field("s1", "select", "Security question *", options=["The street you grew up on", "Your first school"]),
        field("s2", label="Answer to your security question *"),
        field("s3", label="Date of birth", required=True),
        field("s4", label="Username", required=True),
        field("s5", "select", "Country", required=True, options=["Freedonia", "Sylvania"]),
        field("s6", label="How did you hear about us?"),
    ]
    plan = _plan(signup_page([*signup_fields(), *extra]))
    assert plan.state == "ask" and plan.stop == ""
    assert [(q.key, q.why, q.kind, q.secret) for q in plan.questions] == [
        ("s1", "security_question", "choice", False),
        ("s2", "security_question", "text", True),
        ("s3", "sensitive", "text", False),
        ("s4", "username", "text", False),
        ("s5", "unknown_required", "choice", False),
    ]
    assert plan.questions[0].options == ("The street you grew up on", "Your first school") and "How did you hear about us?" in plan.left_blank
    answers = {"s1": "Your first school", "s2": "Fixture Elementary", "s3": "1990-01-01", "s4": "avery-fixture", "s5": "Freedonia"}
    done = _plan(signup_page([*signup_fields(), *extra]), answers=answers)
    assert done.state == "ready" and not done.questions and {item.key: item.source for item in done.typed if item.key.startswith("s")} == dict.fromkeys(answers, "owner")
    shown = json.dumps(done.card()) + json.dumps(done.material()) + repr(done)
    assert "Fixture Elementary" not in shown and next(row for row in done.card()["typed"] if row["label"].startswith("Answer"))["hidden"] is True
    # an answer that is not one of the page's own choices is asked again
    assert [q.key for q in _plan(signup_page([*signup_fields(), *extra]), answers={**answers, "s5": "Elsewhere"}).questions] == ["s5"]


def test_a_form_with_too_many_questions_is_the_owners_to_fill():
    extra = [field(f"x{n}", label=f"Required detail {n}", required=True) for n in range(6)]
    plan = _plan(signup_page([*signup_fields(), *extra]))
    assert plan.state == "stop" and plan.stop == "too_many_questions" and len(plan.questions) == 6 and not plan.typed


# -- what ends the attempt before anything is typed ----------------------------------------------------------
def test_a_human_check_a_block_page_or_a_phone_code_ends_the_attempt():
    for kind in ("recaptcha", "human_check"):
        assert read_page(signup_page(captcha=kind)).stop == "captcha" and _plan(signup_page(captcha=kind)).stop == "captcha"
    blocked = page(text="Sorry, you have been blocked. This site uses a security service to protect itself.", title="Attention required")
    assert read_page(blocked).kind == "blocked" and _plan(blocked).state == "stop" and _plan(blocked).stop == "blocked"
    assert read_page(page(text="Access denied. Unusual traffic from your network.", title="Error")).kind == "blocked"
    phone = signup_fields()
    phone[5] = field("k6", "tel", "Mobile number *", required=True, hint="We will text you a code to verify your phone number.")
    assert _plan(signup_page(phone)).stop == "phone_verification"
    plain = signup_fields()
    plain[5] = field("k6", "tel", "Mobile number *", required=True)
    typed = _plan(signup_page(plain))
    assert typed.state == "ready" and [(i.source, i.value) for i in typed.typed if i.role == "phone"] == [("profile", "555 0100")]
    assert [q.why for q in _plan(signup_page(plain), MORGAN).questions] == ["profile_missing"]  # no phone on file: asked, not invented


def test_pages_that_are_not_a_create_account_form_are_not_planned():
    assert read_page(signin_page()).kind == "signin_form" and _plan(signin_page()).stop == "not_signup"
    assert _plan(page(text="Welcome to our careers site.")).stop == "not_signup"
    email_first = page([field("k1", "email", "Email")], [button("k2", "Sign up")])
    assert read_page(email_first).kind == "email_first" and _plan(email_first).stop == "email_first"
    social = page([], [button("k1", "Continue with Example ID", form=-1, type="")], text="Create your account")
    assert read_page(social).kind == "social_only" and _plan(social).stop == "social_only"
    assert _plan(signup_page(url=f"http://{SITE}/account/new")).stop == "not_secure"
    assert _plan(signup_page(), email="").stop == "no_email"
    assert _plan(signup_page(buttons=[button("k10", "Sign in")])).stop == "no_submit"  # two password boxes, but no button that creates anything
    assert _plan(signup_page(buttons=[button("k10", "Continue with Example ID")])).stop == "no_submit"
    three = [*signup_fields(), field("k20", "password", "Old password")]
    assert _plan(signup_page(three)).stop == "not_signup"


def test_a_page_with_sign_in_and_sign_up_side_by_side_plans_only_the_new_account_form():
    returning = [field("r1", "email", "Email", form=0, autocomplete="username"), field("r2", "password", "Password", form=0, autocomplete="current-password")]
    new = [{**item, "form": 1} for item in signup_fields()]
    both = page([*returning, *new], [button("r3", "Sign in", form=0), button("k10", "Register", form=1)], [link("k12", "Privacy Policy", PRIVACY)])
    assert read_page(both).kind == "signup_form" and read_page(both).form == 1
    plan = _plan(both)
    assert plan.state == "ready" and plan.submit == "k10" and not any(item.key.startswith("r") for item in plan.typed)
    # one password box and a plain button, but the form asks for a name and consent: it is a create-account form
    plain = [field("k1", label="Full name"), field("k3", "email", "Email"), field("k4", "password", "Password"), consent_box()]
    assert read_page(signup_page(plain, buttons=[button("k10", "Continue")])).kind == "signup_form"


def test_the_sites_own_way_to_the_create_account_page_is_found_and_never_one_that_leaves_the_site():
    assert signup_control(signin_page()) == {"key": "k5", "text": "Create an account", "href": URL}
    assert signup_control(signin_page("https://accounts.elsewhere.test/register")) is None
    assert signup_control(signin_page(f"http://{SITE}/account/new")) is None
    pressed = page([], [button("k1", "Sign up", form=-1, type="button")], [link("k2", "Sign up with Example ID", f"https://{SITE}/sso")], url=f"https://{SITE}/login")
    assert signup_control(pressed) == {"key": "k1", "text": "Sign up", "href": ""}
    assert signup_control(page([], [], [link("k1", "Help", f"https://{SITE}/help")])) is None


# -- what the site says after the form is sent ---------------------------------------------------------------
@pytest.mark.parametrize(
    "said",
    [
        "An account with this email address already exists.",
        "This email is already registered. Sign in instead.",
        "That email address is already in use.",
        "Looks like you already have an account. Try signing in.",
        "Email has already been taken",
        "This address is already associated with a profile.",
    ],
)
def test_an_email_that_already_has_an_account_ends_the_attempt(said):
    after = signup_page(text=said)
    assert _sent(after).kind == "email_exists" and _sent(after).stop == "email_exists" and _sent(after).message == said
    assert read_page(signup_page(alerts=[said])).kind == "email_exists"  # also when the page marks it as an alert


def test_the_usual_sign_in_link_is_not_read_as_an_existing_account():
    assert read_page(signup_page()).kind == "signup_form"
    moved = page(text="Welcome aboard.\nAlready have an account? Sign in", url=f"https://{SITE}/welcome")
    assert _sent(moved).kind == "unknown"
    # without `before`, a line the form always prints is not a refusal
    assert read_page(signup_page(text="Your password must be at least 10 characters.")).kind == "signup_form"


def test_a_password_the_site_refuses_is_reported_with_its_words_and_the_rules_they_add():
    said = "Your password must include a special character and be no more than 24 characters."
    seen = _sent(signup_page(text=said))
    assert seen.kind == "password_rejected" and seen.message == said and seen.stop == ""
    assert seen.rules == replace(SIGN_UP_RULES, need_symbol=True, max_length=24)
    breached = _sent(signup_page(text="This password has appeared in a data breach. Choose a different one."))
    assert breached.kind == "password_rejected" and breached.rules == SIGN_UP_RULES
    # the two boxes not matching is the form being refused, not a rule about passwords
    assert _sent(signup_page(text="Passwords do not match.")).kind == "form_refused"
    other = _sent(signup_page(text="Please enter a valid last name."))
    assert other.kind == "form_refused" and other.message == "Please enter a valid last name."
    flagged = signup_fields()
    flagged[3] = {**flagged[3], "invalid": True, "hint": "Password is too short. Use 12 or more characters."}
    assert read_page(signup_page(flagged)).kind == "password_rejected" and read_page(signup_page(flagged)).rules.min_length == 12


def test_an_emailed_code_an_emailed_link_and_a_phone_code_are_told_apart():
    mailed = _sent(code_page("We emailed a 6-digit code to a***@example.test. Enter the code to finish."))
    assert mailed.kind == "email_code" and mailed.code_boxes == ("k1",) and mailed.stop == ""
    split = _sent(code_page("Enter the verification code we sent to your email.", split=True))
    assert split.kind == "email_code" and split.code_boxes == ("k1", "k2", "k3", "k4", "k5", "k6")
    unnamed = _sent(code_page("Enter the 6-digit code to continue."))
    assert unnamed.kind == "code_prompt" and unnamed.stop == ""  # the page does not say where the code went: the owner is asked
    for said in ("We sent a text message with a code to the number ending 0100.", "Enter the code sent by SMS.", "Verify your phone number. Enter the 6-digit code."):
        assert _sent(code_page(said)).kind == "phone_verification" and _sent(code_page(said)).stop == "phone_verification"
    for said in (
        "We sent you a verification link. Check your inbox to finish.",
        "Please verify your email address by clicking the link we sent.",
        "Almost done. Check your email.",
    ):
        linked = _sent(page(text=said, url=f"https://{SITE}/account/pending", title="One more step"))
        assert linked.kind == "email_link" and linked.stop == "" and linked.code_boxes == ()
    # the same words beside a form that is still there are a notice, not the next step
    assert read_page(signup_page(text="We will send a verification link to your email.")).kind == "signup_form"
    assert _sent(signup_page(captcha="recaptcha", text="Please complete the check.")).kind == "captcha"
    assert _sent(page(text="Access denied", title="Access denied", url=f"https://{SITE}/account/new")).kind == "blocked"


def test_a_sender_is_taken_only_from_a_line_that_names_where_the_mail_comes_from():
    said = "We emailed you a code. The email comes from no-reply@mail.hiring-desk.test.\nQuestions? Write to help@support-desk.test\nYou signed up as a***@example.test"
    assert named_senders(code_page(said), EMAIL) == ("hiring-desk.test",)
    assert _sent(code_page(said)).senders == ("hiring-desk.test",)
    assert named_senders(code_page(f"A message from {EMAIL} cannot be right."), EMAIL) == ()
    assert named_senders(signup_page()) == ()


# -- password rules ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "rules"),
    [
        (
            "Password must be at least 10 characters and include an uppercase letter, a lowercase letter, a number and a special character.",
            PasswordRules(min_length=10, need_lower=True, need_upper=True, need_digit=True, need_symbol=True),
        ),
        ("Use 8-64 characters.", PasswordRules(min_length=8, max_length=64)),
        ("Between 12 and 30 characters, with one capital letter.", PasswordRules(min_length=12, max_length=30, need_upper=True)),
        ("Your password is too short. Minimum length is 12.", PasswordRules(min_length=12)),
        ("Password cannot be longer than 20 characters.", PasswordRules(max_length=20)),
        ("Password must be no fewer than 9 characters.", PasswordRules(min_length=9)),
        ("Must be more than 7 characters.", PasswordRules(min_length=8)),
        ("Must be less than 21 characters.", PasswordRules(max_length=20)),
        ("Password must be 12 characters or longer.", PasswordRules(min_length=12)),
        ("Up to 16 characters.", PasswordRules(max_length=16)),
        ("Passwords must be at least 8 characters long and contain at least 1 number.", PasswordRules(min_length=8, need_digit=True)),
        ("Password may only contain letters and numbers.", PasswordRules(need_digit=True, no_symbols=True)),
        ("No special characters, please.", PasswordRules(no_symbols=True)),
        ("Must contain one special character: ! @ # $ % ^ & *", PasswordRules(need_symbol=True, allowed_symbols="!@#$%^&*")),
        ("At least one symbol (!@#$%^&*) and one digit.", PasswordRules(need_digit=True, need_symbol=True, allowed_symbols="!@#$%^&*")),
        ("Include a symbol such as ! or #.", PasswordRules(need_symbol=True)),
        ("Password cannot contain spaces or your e-mail address.", PasswordRules()),
        ("Must not contain < > or &", PasswordRules(forbidden="<>&")),
        ("The password must not contain the characters . or -", PasswordRules(forbidden=".-")),
        ("This password has appeared in a data breach. Choose a different one.", PasswordRules()),
        ("Choose something memorable.", PasswordRules()),
    ],
)
def test_password_rules_are_read_from_the_sites_own_words(text, rules):
    assert rules_from_text(text) == rules


def test_the_rules_come_from_the_box_its_pattern_its_hint_and_the_page_together():
    fields = signup_fields()
    fields[3] = field(
        "k4", "password", "Password", autocomplete="new-password", maxlength=40, pattern=r"(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*]).{12,64}", hint="Use a long password."
    )
    described = signup_page(fields, text="Your password cannot contain the characters < or >.")
    assert page_rules(described, 0) == PasswordRules(
        min_length=12, max_length=40, need_lower=True, need_upper=True, need_digit=True, need_symbol=True, forbidden="<>", allowed_symbols="!@#$%^&*"
    )
    assert page_rules(page([field("k1", label="Email")]), 0) == PasswordRules()
    merged = merge_rules(PasswordRules(min_length=8, max_length=64, allowed_symbols="!@#"), PasswordRules(min_length=12, max_length=30, need_digit=True, allowed_symbols="@#$"))
    assert merged == PasswordRules(min_length=12, max_length=30, need_digit=True, allowed_symbols="@#")


def test_the_recipe_follows_the_rules_and_rules_the_generator_cannot_meet_end_the_attempt():
    def with_hint(hint: str) -> dict:
        fields = signup_fields()
        fields[3] = field("k4", "password", "Password", autocomplete="new-password", hint=hint)
        return signup_page(fields)

    assert _plan(with_hint("8 to 20 characters.")).recipe == "letters,digits,symbols,20"
    assert _plan(with_hint("Letters and numbers only, at least 12 characters.")).recipe == "letters,digits,32"
    assert _plan(with_hint("Must not contain the characters . or -")).recipe == "letters,digits,32"
    # the page names the only marks it accepts and wants one: the generator cannot be held to that list
    listed = _plan(with_hint("Must contain one special character: ! @ # $ % ^ & *"))
    assert listed.state == "stop" and listed.stop == "password_rules" and not listed.typed
    assert _plan(with_hint("Password must be at least 80 characters.")).stop == "password_rules"


# -- the reader, in a real page ------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def browser_page():
    try:
        from playwright.sync_api import sync_playwright

        manager = sync_playwright().start()
        browser = manager.chromium.launch(headless=True)
    except Exception:
        pytest.skip("Playwright Chromium not installed")
    page = browser.new_page()
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=route.request.url.endswith("/account/new") and SIGNUP_HTML or "<html></html>"))
    yield page
    browser.close()
    manager.stop()


def _load(browser_page, html: str, url: str = URL) -> dict:
    browser_page.unroute("**/*")
    browser_page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    browser_page.goto(url)
    return browser_page.evaluate(PAGE_JS)


@pytest.mark.browser
def test_the_reader_describes_a_real_page_and_the_plan_follows(browser_page):
    description = _load(browser_page, SIGNUP_HTML)
    assert description["url"] == URL and description["captcha"] == ""
    labels = [item["label"] for item in description["fields"]]
    assert "Website" not in labels and "First name *" in labels  # a box no person can see is not described
    plan = plan_signup(description, AVERY, EMAIL)
    assert plan.state == "ready" and plan.rules == SIGN_UP_RULES and plan.submit_text == "Create account"
    assert [(item.role, item.source, item.value) for item in plan.typed] == [
        ("first_name", "profile", "Avery"),
        ("last_name", "profile", "Fictional"),
        ("email", "account_email", EMAIL),
        ("password", "password_manager", ""),
        ("password_confirm", "password_manager", ""),
    ]
    assert [(box.kind, box.tick, box.required) for box in plan.boxes] == [("consent", True, True), ("marketing", False, False)]
    assert plan.boxes[0].links == (("Terms of Use", TERMS), ("Privacy Policy", PRIVACY)) and plan.terms == [("Terms of Use", TERMS), ("Privacy Policy", PRIVACY)]
    assert plan.left_blank == ["Mobile number (optional)", "How did you hear about us?", "Contact me about this role by"]
    choice = next(item for item in description["fields"] if item["type"] == "radio")
    assert choice["options"] == ["Email", "A call"] and next(item for item in description["fields"] if item["type"] == "select")["options"] == ["A friend", "A job board"]
    # every key in the plan finds exactly one thing on the page again
    for key in [item.key for item in plan.typed] + [box.key for box in plan.boxes] + [plan.submit]:
        assert browser_page.locator(f'[{SIGNUP_MARK}="{key}"]').count() == 1
    assert browser_page.locator(f'[{SIGNUP_MARK}="{plan.submit}"]').inner_text() == "Create account"
    assert browser_page.locator(f'[{SIGNUP_MARK}="{plan.typed[3].key}"]').get_attribute("name") == "password"


@pytest.mark.browser
def test_the_reader_sees_a_rejection_a_code_prompt_and_a_human_check(browser_page):
    before = page_lines(_load(browser_page, SIGNUP_HTML))
    refused = read_page(_load(browser_page, REJECTED_HTML), before=before)
    assert refused.kind == "password_rejected" and refused.rules == replace(SIGN_UP_RULES, need_symbol=True, max_length=24)
    assert refused.message == "Your password must include a special character and be no more than 24 characters."
    prompt = read_page(_load(browser_page, CODE_HTML, f"https://{SITE}/account/verify"), before=before, account_email=EMAIL)
    assert prompt.kind == "email_code" and len(prompt.code_boxes) == 6 and prompt.senders == ("hiring-desk.test",)
    assert read_page(_load(browser_page, WIDGET_HTML)).kind == "captcha"
