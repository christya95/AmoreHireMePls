"""Recognising a job site's create-account page, and saying what would be typed and ticked on it.

Everything here is a pure function over a description of a page: the fields, boxes, buttons, links and words a
person would see. Nothing here opens a page, types, ticks or presses, and no site is named. `PAGE_JS` is the reader
that makes the description inside a browser; the step that acts on a plan is not part of this module.

Two questions are answered:

  `read_page(description, before)`   what is this page? A create-account form, a sign-in form, a page asking for an
                                     emailed code, a page saying a link was emailed, a refusal (the email already
                                     has an account, the password broke a rule, the form was refused), or something
                                     that ends the attempt (a human check, a phone code, a block page).
  `plan_signup(description, ...)`    what would be typed and ticked on this form? The plan is for the owner's
                                     approval card. It is `ready`, or it has questions only the owner can answer
                                     (`ask`), or it names why the attempt ends (`stop`).

Rules the plan keeps:
  * the account email comes from the caller and the name from the profile's own values; nothing is made up
  * the password is never in a plan: its place is marked `password_manager` and filled at the owner's press
  * a box that agrees to the terms or the privacy notice is ticked; a marketing box is never ticked; a box that
    joins the two, or a required box of any other kind, is a question for the owner
  * a required field the profile cannot answer (a security question, a date of birth, a username) is a question
  * a human check, a phone code, a block page, or a page that is not secure ends the attempt

`before` is the set of lines the page showed before the form was sent (`page_lines`). With it, a refusal is read
only from what is new on the page, so a rule the form always prints is not mistaken for a rejection.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from amorehiremepls.apply.browser import CHALLENGE_SELECTORS
from amorehiremepls.core.account_items import PasswordRules, RulesImpossible, recipe_for
from amorehiremepls.core.credentials import registrable_domain, secure_page, site_host

SIGNUP_MARK = "data-amore-signup"
MAX_QUESTIONS = 5  # more than this and the form is the owner's to fill
TEXT_CAP = 8000
MESSAGE_CAP = 300
MAX_OPTIONS = 60

# Human-check widgets that sit on a form before it is sent, beside the open challenges the browser already knows.
_WIDGETS = (
    'iframe[src*="recaptcha"][src*="anchor"]:not([src*="size=invisible"])',
    'iframe[src*="hcaptcha.com"][src*="frame=checkbox"]',
    "[data-captcha-widget]",
)
_VISIBLE = (
    "(el) => { const s = window.getComputedStyle(el); if (el.hidden || s.display === 'none' || s.visibility === 'hidden') return false;"
    " const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }"
)
# Describes the page and marks every field, button and link it names, so a later step can find them again by key.
PAGE_JS = (
    """() => {
  const visible = __VISIBLE__;
  const MARK = '__MARK__';
  for (const el of document.querySelectorAll('[' + MARK + ']')) el.removeAttribute(MARK);
  const squash = (s, cap) => ((s || '') + '').replace(/\\s+/g, ' ').trim().slice(0, cap || 300);
  let count = 0;
  const mark = (el) => { const key = 'k' + (++count); el.setAttribute(MARK, key); return key; };
  const forms = [...document.forms];
  const texts = (ids) => (ids || '').split(/\\s+/).filter(Boolean).map(id => document.getElementById(id)).filter(Boolean).map(el => el.innerText);
  const labelOf = (el) => {
    const parts = [...(el.labels ? [...el.labels].map(l => l.innerText) : []), ...texts(el.getAttribute('aria-labelledby'))];
    if (el.getAttribute('aria-label')) parts.push(el.getAttribute('aria-label'));
    if (!parts.length && (el.type === 'checkbox' || el.type === 'radio') && el.parentElement) parts.push(el.parentElement.innerText);
    return squash(parts.join(' '));
  };
  const linksIn = (el) => {
    const roots = [...(el.labels ? [...el.labels] : [])];
    if (!roots.length && el.parentElement) roots.push(el.parentElement);
    return roots.flatMap(root => [...root.querySelectorAll('a[href]')]).slice(0, 6).map(a => ({text: squash(a.innerText, 120), href: a.href}));
  };
  const number = (el, name) => { const n = parseInt(el.getAttribute(name) || '', 10); return Number.isFinite(n) && n > 0 ? n : 0; };
  const fields = [];
  const groups = {};
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text').toLowerCase() : el.tagName.toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type) || !visible(el) || el.disabled) continue;
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio' && el.name) {
      const key = forms.indexOf(el.form) + ':' + el.name;
      if (groups[key]) {
        groups[key].options.push(labelOf(el));
        groups[key].required = groups[key].required || required;
        groups[key].checked = groups[key].checked || el.checked;
        continue;
      }
    }
    const group = type === 'radio' ? el.closest('fieldset, [role=radiogroup]') : null;
    const legend = group ? (group.querySelector('legend') || {}).innerText || group.getAttribute('aria-label') || '' : '';
    const item = {
      key: mark(el), type: type, form: forms.indexOf(el.form), name: el.name || '', id: el.id || '',
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      label: type === 'radio' ? squash(legend) : labelOf(el), placeholder: squash(el.placeholder, 120),
      required: required, minlength: number(el, 'minlength'), maxlength: number(el, 'maxlength'),
      pattern: (el.getAttribute('pattern') || '').slice(0, 300),
      hint: squash(texts(el.getAttribute('aria-describedby')).join(' '), 600),
      invalid: el.getAttribute('aria-invalid') === 'true',
      checked: !!el.checked, filled: type !== 'password' && !['checkbox', 'radio'].includes(type) && !!el.value,
      options: type === 'select' ? [...el.options].filter(o => o.value !== '').slice(0, __OPTIONS__).map(o => squash(o.text, 80))
        : type === 'radio' ? [labelOf(el)] : [],
      links: type === 'checkbox' ? linksIn(el) : [],
    };
    if (type === 'radio' && el.name) groups[forms.indexOf(el.form) + ':' + el.name] = item;
    fields.push(item);
  }
  const buttons = [...document.querySelectorAll('button, input[type=submit], [role=button]')].filter(el => visible(el) && !el.disabled).slice(0, 40)
    .map(el => ({key: mark(el), form: forms.indexOf(el.form || el.closest('form')), type: (el.getAttribute('type') || '').toLowerCase(),
      text: squash(el.innerText || el.value || el.getAttribute('aria-label'), 120)}));
  const links = [...document.querySelectorAll('a[href]')].filter(visible).slice(0, 80)
    .map(el => ({key: mark(el), text: squash(el.innerText || el.getAttribute('aria-label'), 120), href: el.href}));
  const alerts = [...document.querySelectorAll('[role=alert], [aria-live=assertive], [aria-live=polite]')].filter(visible)
    .map(el => squash(el.innerText, 400)).filter(Boolean).slice(0, 10);
  const shows = (selector) => { try { return [...document.querySelectorAll(selector)].some(visible); } catch (e) { return false; } };
  const open = __CHALLENGES__.find(pair => shows(pair[1]));
  return {
    url: location.href, title: document.title || '', text: (document.body ? document.body.innerText : '').slice(0, __TEXT__),
    fields: fields, buttons: buttons, links: links, alerts: alerts,
    captcha: open ? open[0] : (__WIDGETS__.some(shows) ? 'human_check' : ''),
  };
}""".replace("__VISIBLE__", _VISIBLE)
    .replace("__MARK__", SIGNUP_MARK)
    .replace("__CHALLENGES__", json.dumps([list(pair) for pair in CHALLENGE_SELECTORS]))
    .replace("__WIDGETS__", json.dumps(list(_WIDGETS)))
    .replace("__OPTIONS__", str(MAX_OPTIONS))
    .replace("__TEXT__", str(TEXT_CAP))
)

# -- words ---------------------------------------------------------------------------------------------------
_UP = re.compile(r"sign ?up|create (an |your |my |a |a new |new )?(account|profile|login)|\bregister\b|\bjoin( now| us| today)?$|get started", re.I)
_IN = re.compile(r"sign ?in|log ?in", re.I)
_ONWARD = re.compile(r"^(continue|next|submit)\b", re.I)
_WITH = re.compile(r"(continue|sign ?(in|up)|log ?in|register|join) (with|using) \w+", re.I)
_BLOCK = re.compile(
    r"you have been blocked|unable to access|security service to protect|access (to this page )?(has been |is |was )?denied|unusual (activity|traffic)|"
    r"automated (requests|queries|access|traffic)|too many requests|request (has been |was )?blocked",
    re.I,
)
_EXISTS = re.compile(
    r"already (has|have) an account(?!\s*\?)|already (been )?(registered|in use|exists?|taken|associated|linked|signed up)|"
    r"account (with (this|that) (e-?mail|address)( address)? )?already exists|(e-?mail|address|user)( address)? (is |has been |was )?(already )?(taken|in use)|"
    r"is already (a member|on file)|(is|was) (registered|in use) (already|with (an|another) account)",
    re.I,
)
_PHONE_VERIFY = re.compile(
    r"(verify|verification|confirm|validate) (of )?(your |the |a )?(phone|mobile|cell)( phone)?( number)?|"
    r"code (sent |delivered )?(by|via|through|in a) (text|sms)|(sent|send|sending) (you )?(a |an |the )?(text|sms)|text(ed)? (you )?(a|an|the|your) (\w+ )?(code|pin)|"
    r"(sent|send|sending) (a |an |the |your )?(\w+ ){0,2}(code|pin) to (your |the )?(phone|mobile|cell|number)|(phone|mobile|cell)( phone)?( number)? ending|"
    r"text message|\bsms\b",
    re.I,
)
_A_CODE = r"((\d|four|five|six|seven|eight)[- ]digit )?(verification |confirmation |security |one[- ]time |access )?(code|passcode|pin)"
_CODE_SENT = re.compile(
    rf"(enter|type|input|provide) (the |your |a )?{_A_CODE}|(sent|emailed|e-mailed|send|sending) (you |it )?(a |an |the |your )?{_A_CODE}|"
    r"(code|passcode|pin) (was|has been|is being|we) (sent|emailed|e-mailed)",
    re.I,
)
_LINK_SENT = re.compile(
    r"(sent|emailed|e-mailed) (you )?(a|an|the|your) (\w+ )?(link|e-?mail|message)|check your (e-?mail|inbox|mail)|"
    r"(verify|confirm|activate|validate) your (e-?mail|account|address)[^.]{0,80}(link|e-?mail|inbox)|(click|follow|open|use) (on )?the link",
    re.I,
)
_MAIL = re.compile(r"e-?mail|inbox|@", re.I)
_PASSWORD_WORD = re.compile(r"password|passphrase", re.I)
_MATCH = re.compile(r"do(es)?(n.t| not) match|must match|must be the same|mismatch|are not the same|not identical", re.I)
_RULE_WORD = re.compile(
    r"must|at least|too (short|long|weak|common|simple|easy)|not (strong|long|valid|allowed|secure|complex)|does(n.t| not) meet|requirement|should|needs? to|cannot|can.t|"
    r"invalid|weak|minimum|maximum|include|contain|characters|stronger|compromised|breach|previously used",
    re.I,
)
_EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_SENDER_LINE = re.compile(r"\bfrom\b|sender|sent by|safe senders?|address book|contacts", re.I)
_STAR = re.compile(r"(\*|\(required\))\s*$", re.I)
_OPTIONAL = re.compile(r"\(optional\)|\boptional\b", re.I)

_TEXT_TYPES = ("text", "email", "tel", "search", "url", "number", "")
_ROLE_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("security_answer", re.compile(r"(security|secret|challenge) ?(answer|response)|answer to", re.I)),
    ("security_question", re.compile(r"(security|secret|challenge) ?question|mother|maiden|first pet|childhood", re.I)),
    ("other", re.compile(r"company|employer|organi[sz]ation|business|school|university|\breferr|invit|promo|coupon|discount|job ?title|website|\burl\b", re.I)),
    ("username", re.compile(r"user.?name|user.?id|login ?(name|id)|screen.?name|display.?name|nick.?name|\bhandle\b", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("first_name", re.compile(r"first.?name|given.?name|\bfname\b|forename", re.I)),
    ("last_name", re.compile(r"last.?name|family.?name|surname|\blname\b", re.I)),
    ("middle_name", re.compile(r"middle", re.I)),
    ("preferred_name", re.compile(r"preferred|goes by|known as", re.I)),
    ("birth_date", re.compile(r"birth|\bdob\b|\bage\b", re.I)),
    ("phone", re.compile(r"phone|mobile|\btel\b|\bcell\b", re.I)),
    ("location", re.compile(r"country|region|province|\bstate\b|\bcity\b|\btown\b|address|zip|postal|post ?code|location|time ?zone|language", re.I)),
    ("full_name", re.compile(r"\bname\b", re.I)),
)
_AUTOCOMPLETE = {
    "email": "email",
    "username": "email",
    "given-name": "first_name",
    "family-name": "last_name",
    "additional-name": "middle_name",
    "name": "full_name",
    "nickname": "preferred_name",
    "tel": "phone",
    "tel-national": "phone",
    "one-time-code": "code",
    "bday": "birth_date",
    "country": "location",
    "country-name": "location",
    "postal-code": "location",
    "street-address": "location",
    "address-level1": "location",
    "address-level2": "location",
    "organization": "other",
}
_CODE_BOX = re.compile(r"code|\botp\b|\bpin\b|one.?time", re.I)
_NOT_A_CODE = re.compile(r"zip|postal|post ?code|promo|coupon|discount|referr|invit|country|area|dial", re.I)
_MARKETING = re.compile(
    r"news|marketing|promot|\boffers?\b|\bupdates?\b|alerts?\b|communications?|talent (community|network|pool)|subscribe|recommend|\btips\b|events|\bsms\b|text messages?|"
    r"contact me|keep me (informed|posted|updated|up to date)|hear (from|about)|mailing list|surveys?|partners?|third.part|affiliates|share (my|your)|"
    r"(future|other) (roles|jobs|opportunities|openings)",
    re.I,
)
_CORE_CONSENT = re.compile(
    r"terms|conditions|privacy|\bpolicy\b|\bpolicies\b|user agreement|\beula\b|data protection|\bgdpr\b|candidate (notice|agreement)|applicant (notice|agreement)", re.I
)
_REMEMBER = re.compile(r"remember me|keep me (signed|logged) in|stay (signed|logged) in|trust this (device|browser|computer)|show (the |my )?password", re.I)
_TERMS_LINK = re.compile(r"terms|conditions|privacy|policy|agreement|notice", re.I)
_YES = ("yes", "true", "on", "tick", "1")

KINDS = (
    "blocked",
    "captcha",
    "email_exists",
    "phone_verification",
    "email_code",
    "code_prompt",
    "email_link",
    "password_rejected",
    "form_refused",
    "signup_form",
    "email_first",
    "social_only",
    "signin_form",
    "unknown",
)
# The page kinds that end the attempt wherever they appear. The others are a form to plan, a refusal the caller
# acts on (a new password, the owner), or a prompt for the emailed code or link.
STOPS = ("blocked", "captcha", "phone_verification", "email_exists", "email_first", "social_only")


# -- reading the page ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SignUpPage:
    """What one page is. `message` is the site's own words for a refusal, shortened; it holds nothing typed."""

    kind: str
    host: str = ""
    message: str = ""
    rules: PasswordRules = field(default_factory=PasswordRules)  # what the page says about passwords, with any rejection folded in
    form: int = -1  # which form on the page is the create-account form
    code_boxes: tuple[str, ...] = ()  # keys of the box or boxes a code is typed into
    captcha: str = ""
    senders: tuple[str, ...] = ()  # mail domains the page itself names as the sender of its verification mail

    @property
    def stop(self) -> str:
        return self.kind if self.kind in STOPS else ""


def _squash(value: Any, cap: int = 300) -> str:
    return " ".join(str(value or "").split())[:cap]


def _fields(description: dict) -> list[dict]:
    return [item for item in description.get("fields") or [] if isinstance(item, dict) and item.get("key")]


def page_lines(description: dict) -> set[str]:
    """The lines a page shows, to pass as `before` once the form has been sent."""
    text = str(description.get("text") or "")[:TEXT_CAP]
    return {line for line in (_squash(raw) for raw in text.splitlines()) if line}


def _fresh(description: dict, before: Iterable[str]) -> list[str]:
    """What the page says now that it did not say before: alerts, hints of fields marked invalid, and new lines."""
    seen = set(before or ())
    said = [_squash(alert, 400) for alert in description.get("alerts") or []]
    said += [_squash(item.get("hint"), 400) for item in _fields(description) if item.get("invalid")]
    if seen:
        said += sorted(page_lines(description) - seen)
    return list(dict.fromkeys(line for line in said if line and line not in seen))


def _shown(item: dict) -> str:
    return " ".join(str(item.get(part) or "") for part in ("label", "placeholder")).lower()


def _inner(item: dict) -> str:
    return " ".join(str(item.get(part) or "") for part in ("name", "id")).lower()


def _is_code_box(item: dict) -> bool:
    words = f"{_shown(item)} {_inner(item)}"
    return "one-time-code" in str(item.get("autocomplete") or "").lower() or (bool(_CODE_BOX.search(words)) and not _NOT_A_CODE.search(words))


def field_role(item: dict) -> str:
    """What one text box, list or choice is for. `other` means the page asks something this module has no rule for.

    The field's own hint to the browser decides first, then the words a person sees beside it, then its name in the page."""
    kind = str(item.get("type") or "").lower()
    if kind == "password":
        return "password"
    if kind == "checkbox":
        return "box"
    tokens = str(item.get("autocomplete") or "").lower().split()
    named = next((_AUTOCOMPLETE[token] for token in tokens if token in _AUTOCOMPLETE), "")
    if named:
        return named
    for words in (_shown(item), _inner(item)):
        role = next((role for role, pattern in _ROLE_WORDS if pattern.search(words)), "")
        if role:
            return role
        if _CODE_BOX.search(words) and not _NOT_A_CODE.search(words):
            return "code"
    return "email" if kind == "email" else "other"


def is_required(item: dict) -> bool:
    label = str(item.get("label") or "")
    return (bool(item.get("required")) or bool(_STAR.search(label))) and not _OPTIONAL.search(label)


def box_kind(item: dict) -> str:
    """consent (agrees to the terms or the privacy notice), marketing, bundled (both in one box), remember, or other."""
    label = str(item.get("label") or "")
    if _REMEMBER.search(label):
        return "remember"
    marketing, consent = bool(_MARKETING.search(label)), bool(_CORE_CONSENT.search(label))
    if marketing and consent:
        return "bundled"
    return "marketing" if marketing else "consent" if consent else "other"


def _code_boxes(description: dict) -> tuple[str, ...]:
    """The box a code is typed into, or the row of one-character boxes some pages use."""
    typed = [item for item in _fields(description) if str(item.get("type") or "").lower() in _TEXT_TYPES]
    single = [item["key"] for item in typed if item.get("maxlength") == 1]
    if 4 <= len(single) <= 8:
        return tuple(single)
    return tuple(item["key"] for item in typed if _is_code_box(item))[:1]


def _signup_form(description: dict) -> tuple[int, str]:
    """Which form is the create-account form, and whether the page is that, a sign-in form, or neither."""
    fields, buttons = _fields(description), [item for item in description.get("buttons") or [] if isinstance(item, dict)]
    by_form: dict[int, list[dict]] = {}
    for item in fields:
        by_form.setdefault(int(item.get("form", -1)), []).append(item)
    sign_in = False
    for index, members in by_form.items():
        passwords = [item for item in members if str(item.get("type") or "").lower() == "password"]
        if not passwords:
            continue
        texts = [str(button.get("text") or "") for button in buttons if int(button.get("form", -1)) == index]
        new = any("new-password" in str(item.get("autocomplete") or "").lower() for item in passwords)
        current = any("current-password" in str(item.get("autocomplete") or "").lower() for item in passwords)
        up, enter = any(_UP.search(text) for text in texts), any(_IN.search(text) and not _UP.search(text) for text in texts)
        asks_more = any(field_role(item) in ("first_name", "last_name", "full_name") or (field_role(item) == "box" and box_kind(item) == "consent") for item in members)
        if len(passwords) >= 2 or ((new or up or asks_more) and not enter and not current):
            return index, "signup_form"
        sign_in = True
    if sign_in:
        return -1, "signin_form"
    for index, members in by_form.items():
        typed = [item for item in members if str(item.get("type") or "").lower() in _TEXT_TYPES]
        texts = [str(button.get("text") or "") for button in buttons if int(button.get("form", -1)) == index]
        if len(typed) == 1 and field_role(typed[0]) == "email" and any(_UP.search(text) for text in texts) and not any(_IN.search(text) and not _UP.search(text) for text in texts):
            return index, "email_first"
    controls = [*buttons, *(link for link in description.get("links") or [] if isinstance(link, dict))]
    if not fields and any(_WITH.search(str(control.get("text") or "")) for control in controls):
        return -1, "social_only"  # the page offers only another service's sign-in: there is no password to make
    return -1, "unknown"


def named_senders(description: dict, account_email: str = "") -> tuple[str, ...]:
    """Mail domains the page names as where its mail comes from ("an email from ..."). The account's own is left out."""
    own = registrable_domain(account_email.rsplit("@", 1)[-1]) if "@" in account_email else ""
    lines = [line for line in page_lines(description) if _SENDER_LINE.search(line)]
    found = [registrable_domain(match.group(1)) for line in sorted(lines) for match in _EMAIL_ADDRESS.finditer(line)]
    return tuple(dict.fromkeys(domain for domain in found if domain and domain != own))[:3]


def read_page(description: dict, before: Iterable[str] = (), account_email: str = "") -> SignUpPage:
    """What this page is. Pass `before` (from `page_lines`) once the form has been sent, so only what is new counts."""
    before = set(before or ())
    host = site_host(str(description.get("url") or ""))
    text = str(description.get("text") or "")[:TEXT_CAP]
    title = str(description.get("title") or "")
    fresh = _fresh(description, before)
    said = " \n ".join(fresh)
    form, shape = _signup_form(description)
    members = [item for item in _fields(description) if int(item.get("form", -1)) == form] if shape == "signup_form" else []
    rules = page_rules(description, form) if shape == "signup_form" else PasswordRules()
    common = {"host": host, "form": form, "senders": named_senders(description, account_email)}
    has_password = any(str(item.get("type") or "").lower() == "password" for item in _fields(description))
    if _BLOCK.search(title) or (not has_password and _BLOCK.search(text[:1500])) or _BLOCK.search(said):
        return SignUpPage("blocked", message=_message(fresh, _BLOCK), **common)
    captcha = str(description.get("captcha") or "")
    if captcha:
        return SignUpPage("captcha", captcha=captcha, rules=rules, **common)
    if _EXISTS.search(said):
        return SignUpPage("email_exists", message=_message(fresh, _EXISTS), **common)
    boxes = _code_boxes(description)
    told = said if before else text
    by_mail, by_phone = bool(_MAIL.search(told)), bool(_PHONE_VERIFY.search(told))
    if shape != "signup_form" and not has_password and boxes and (_CODE_SENT.search(told) or by_phone):
        if by_phone and not by_mail:
            return SignUpPage("phone_verification", **common)
        return SignUpPage("email_code" if by_mail else "code_prompt", code_boxes=boxes, **common)
    if shape != "signup_form" and not has_password and by_phone and not by_mail and re.search(r"\b(code|pin)\b", told, re.I):
        return SignUpPage("phone_verification", **common)
    if shape != "signup_form" and not has_password and not boxes and _LINK_SENT.search(told):
        return SignUpPage("email_link", **common)
    if shape == "signup_form":
        about_password = [line for line in fresh if _PASSWORD_WORD.search(line) and _RULE_WORD.search(line) and not _MATCH.search(line)]
        about_password += [_squash(item.get("hint"), 400) for item in members if item.get("invalid") and str(item.get("type") or "").lower() == "password" and item.get("hint")]
        if about_password:
            message = _squash(" ".join(dict.fromkeys(about_password)), MESSAGE_CAP)
            return SignUpPage("password_rejected", message=message, rules=merge_rules(rules, rules_from_text(message)), **common)
        if before and fresh:
            return SignUpPage("form_refused", message=_squash(" ".join(fresh), MESSAGE_CAP), rules=rules, **common)
        return SignUpPage("signup_form", rules=rules, **common)
    return SignUpPage(shape, **common)


def _message(lines: list[str], pattern: re.Pattern[str]) -> str:
    return _squash(" ".join(line for line in lines if pattern.search(line)), MESSAGE_CAP)


def signup_control(description: dict) -> dict | None:
    """On a sign-in page, the site's own control that leads to creating an account: `{key, text, href}`, or None.

    A link is offered only when it stays on the same site and is a secure page. Nothing is pressed here."""
    host = site_host(str(description.get("url") or ""))
    for link in description.get("links") or []:
        text, href = str(link.get("text") or ""), str(link.get("href") or "")
        if _UP.search(text) and not _WITH.search(text) and secure_page(href) and registrable_domain(href) == registrable_domain(host):
            return {"key": str(link.get("key") or ""), "text": _squash(text, 120), "href": href}
    for button in description.get("buttons") or []:
        text = str(button.get("text") or "")
        if _UP.search(text) and not _WITH.search(text) and int(button.get("form", -1)) == -1:
            return {"key": str(button.get("key") or ""), "text": _squash(text, 120), "href": ""}
    return None


# -- what a page says about passwords ------------------------------------------------------------------------
_RANGE = re.compile(r"(?:between\s+)?(\d{1,3})\s*(?:-|–|to|and)\s*(\d{1,3})\s*char", re.I)
_MIN = (
    re.compile(r"(?:at least|minimum(?: of)?|min\.?)\s*(\d{1,3})\s*char", re.I),
    re.compile(r"(\d{1,3})\s*(?:or more|\+)\s*char", re.I),
    re.compile(r"(\d{1,3})\s*characters?\s*(?:or more|or longer|or greater|minimum|at least|at a minimum)", re.I),
    re.compile(r"min(?:imum)?\.?\s*(?:password\s*)?length(?:\s*(?:of|is|:))?\s*(\d{1,3})", re.I),
)
_MAX = (
    re.compile(r"(?:at most|maximum(?: of)?|max\.?|up to|exceed)\s*(\d{1,3})\s*char", re.I),
    re.compile(r"(\d{1,3})\s*characters?\s*(?:or (?:fewer|less|shorter)|maximum|max\b|at most)", re.I),
    re.compile(r"max(?:imum)?\.?\s*(?:password\s*)?length(?:\s*(?:of|is|:))?\s*(\d{1,3})", re.I),
)
_COMPARE = re.compile(r"(more|longer|greater|fewer|less|shorter) than\s*(\d{1,3})\s*char", re.I)
_NEGATED = re.compile(r"\b(no|not|cannot|never)\b|n.t\b", re.I)
_NO_SYMBOLS = re.compile(
    r"(no|not|cannot|can.t|must not|may not|should not|without)( contain| include| use| have)?( any)? (special characters|symbols|punctuation)|"
    r"(only|just)( contain| use| include)? letters and (numbers|digits)|letters and (numbers|digits) only|alphanumeric( characters)? only|only alphanumeric",
    re.I,
)
_SYMBOL = re.compile(r"special character|symbol|punctuation|non.?alphanumeric", re.I)
_SYMBOL_TAIL = re.compile(r"(?:special characters?|symbols?|punctuation)(.{0,80})", re.I)
_EXAMPLES = re.compile(r"such as|\blike\b|e\.g|for example|including|\betc\b", re.I)
_BAN_TAIL = re.compile(r"(?:cannot|can.t|must not|may not|should not|not allowed to)\s*(?:contain|include|use|have)(.{0,80})", re.I)
_MARKS = re.compile(r"(?:(?<=[\s:(])|^)((?:[^\sA-Za-z0-9]+[\s,]*(?:or\s+|and\s+)?)+)")


def _marks(tail: str, least: int) -> str:
    """Marks that stand on their own after a phrase: not a hyphen inside a word, not the full stop that ends it."""
    tail = tail.lstrip(" :")
    found = _MARKS.search(tail)
    if not found:
        return ""
    run = re.sub(r"\b(or|and)\b", "", found.group(1)).strip()
    run = run[:-1] if run.endswith(".") and (len(run) == 1 or not tail[found.end() :].strip()) else run
    marks = "".join(dict.fromkeys(char for char in run if not char.isalnum() and not char.isspace() and char not in ",()"))
    return marks if len(marks) >= least else ""


def _length(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if 4 <= number <= 256 else 0


def rules_from_text(text: str) -> PasswordRules:
    """Password rules stated in words: a hint beside the box, or the site's rejection. Unknown wording changes nothing."""
    text = _squash(text, 2000)
    low, high = 0, 0
    span = _RANGE.search(text)
    if span and int(span.group(1)) < int(span.group(2)):
        low, high = _length(span.group(1)), _length(span.group(2))
    low = low or next((_length(m.group(1)) for m in (pattern.search(text) for pattern in _MIN) if m and _length(m.group(1))), 0)
    high = high or next((_length(m.group(1)) for m in (pattern.search(text) for pattern in _MAX) if m and _length(m.group(1))), 0)
    for found in _COMPARE.finditer(text):  # "no more than 20" is a ceiling, "more than 8" a floor, and the reverse for "less than"
        number, over = _length(found.group(2)), found.group(1).lower() in ("more", "longer", "greater")
        negated = bool(_NEGATED.search(text[max(0, found.start() - 25) : found.start()]))
        if not number:
            continue
        if over == negated:
            high = high or (number if negated else number - 1)
        else:
            low = low or (number if negated else number + 1)
    no_symbols = bool(_NO_SYMBOLS.search(text))
    allowed, forbidden = "", ""
    banned = _BAN_TAIL.search(text)
    if banned and not no_symbols:
        forbidden = _marks(banned.group(1), 1)
    listed = _SYMBOL_TAIL.search(text)
    if listed and not no_symbols and not forbidden and not _EXAMPLES.search(listed.group(1)):  # "such as" names examples; a bare list names the only marks accepted
        allowed = _marks(listed.group(1), 3)
    return PasswordRules(
        min_length=low,
        max_length=high if not low or high >= low else 0,
        need_lower=bool(re.search(r"lower.?case", text, re.I)),
        need_upper=bool(re.search(r"upper.?case|capital", text, re.I)),
        need_digit=bool(re.search(r"\bnumbers?\b|\bdigits?\b|numeric|\bnumerals?\b", text, re.I)),
        need_symbol=bool(_SYMBOL.search(text)) and not no_symbols,
        no_symbols=no_symbols,
        forbidden=forbidden,
        allowed_symbols=allowed,
    )


def _rules_from_pattern(pattern: str) -> PasswordRules:
    """The few things a field's own pattern says plainly: a length at its end and the usual "must contain" checks."""
    if not pattern:
        return PasswordRules()
    span = re.search(r"\{(\d{1,3}),(\d{0,3})\}\$?\)?$", pattern)
    looks = re.findall(r"\(\?=[^)]*?(\[[^\]]+\]|\\d|\\W)", pattern)
    marks = next((look for look in looks if look.startswith("[") and not look.startswith("[^") and not re.search(r"[A-Za-z0-9]", look.replace("\\", ""))), "")
    return PasswordRules(
        min_length=_length(span.group(1)) if span else 0,
        max_length=_length(span.group(2)) if span and span.group(2) else 0,
        need_lower=any("a-z" in look for look in looks if not look.startswith("[^")),
        need_upper=any("A-Z" in look for look in looks if not look.startswith("[^")),
        need_digit=any(look == "\\d" or "0-9" in look or "\\d" in look for look in looks if not look.startswith("[^")),
        need_symbol=bool(marks) or any(look == "\\W" or look.startswith("[^") for look in looks),
        allowed_symbols="".join(dict.fromkeys(char for char in marks[1:-1] if char != "\\")) if marks else "",
    )


def merge_rules(*many: PasswordRules) -> PasswordRules:
    """Every stated rule at once: the longest minimum, the shortest maximum, every need, every ban."""
    out = PasswordRules()
    for rules in many:
        allowed = (
            "".join(char for char in out.allowed_symbols if char in rules.allowed_symbols)
            if out.allowed_symbols and rules.allowed_symbols
            else out.allowed_symbols or rules.allowed_symbols
        )
        out = replace(
            out,
            min_length=max(out.min_length, rules.min_length),
            max_length=min(value for value in (out.max_length, rules.max_length) if value) if out.max_length or rules.max_length else 0,
            need_lower=out.need_lower or rules.need_lower,
            need_upper=out.need_upper or rules.need_upper,
            need_digit=out.need_digit or rules.need_digit,
            need_symbol=out.need_symbol or rules.need_symbol,
            no_symbols=out.no_symbols or rules.no_symbols,
            forbidden="".join(dict.fromkeys(out.forbidden + rules.forbidden)),
            allowed_symbols=allowed,
        )
    return out


def page_rules(description: dict, form: int = -1) -> PasswordRules:
    """What the page says about passwords: the box's own limits and pattern, its hint, and the lines that state rules."""
    boxes = [item for item in _fields(description) if str(item.get("type") or "").lower() == "password" and int(item.get("form", -1)) == form]
    if not boxes:
        return PasswordRules()
    first = boxes[0]
    own = PasswordRules(min_length=_length(first.get("minlength")), max_length=_length(first.get("maxlength")))
    lines = [line for line in page_lines(description) if _PASSWORD_WORD.search(line) and _RULE_WORD.search(line) and not _MATCH.search(line)]
    stated = [rules_from_text(text) for text in [str(first.get("hint") or ""), *sorted(lines)] if text]
    return merge_rules(own, _rules_from_pattern(str(first.get("pattern") or "")), *stated)


# -- the plan ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PlannedField:
    """One box that would be typed into. The password has no value here: it arrives sealed at the owner's press."""

    key: str
    role: str
    label: str
    source: str  # account_email | profile | password_manager | owner
    value: str = field(default="", repr=False)
    secret: bool = False


@dataclass(frozen=True)
class PlannedBox:
    """One tick box. `tick` is the state it is left in: a marketing box that arrives ticked is cleared."""

    key: str
    label: str
    kind: str  # consent | marketing | bundled | remember | other
    tick: bool
    required: bool
    links: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Question:
    """Something only the owner can answer before the form may be sent."""

    key: str
    label: str
    why: str  # security_question | sensitive | username | profile_missing | bundled_consent | required_box | unknown_required
    kind: str = "text"  # text | choice | box
    options: tuple[str, ...] = ()
    secret: bool = False  # the answer is typed but never shown again or kept in a plan's summary


@dataclass
class SignUpPlan:
    """What would be typed and ticked on one create-account form, for the owner's approval card."""

    state: str  # ready | ask | stop
    stop: str = ""
    host: str = ""
    typed: list[PlannedField] = field(default_factory=list)
    boxes: list[PlannedBox] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    left_blank: list[str] = field(default_factory=list)  # optional fields that stay empty
    rules: PasswordRules = field(default_factory=PasswordRules)
    recipe: str = ""
    submit: str = ""
    submit_text: str = ""
    terms: list[tuple[str, str]] = field(default_factory=list)
    message: str = ""

    def card(self) -> dict:
        """The plan in the owner's terms. A secret answer and the password are named, never shown."""
        return {
            "state": self.state,
            "stop": self.stop,
            "host": self.host,
            "typed": [
                {
                    "label": item.label,
                    "role": item.role,
                    "source": item.source,
                    "value": "" if item.secret or item.source == "password_manager" else item.value,
                    "hidden": item.secret,
                }
                for item in self.typed
            ],
            "ticked": [{"label": box.label, "links": [list(link) for link in box.links]} for box in self.boxes if box.tick],
            "not_ticked": [{"label": box.label, "kind": box.kind} for box in self.boxes if not box.tick],
            "questions": [{"key": q.key, "label": q.label, "why": q.why, "kind": q.kind, "options": list(q.options), "secret": q.secret} for q in self.questions],
            "left_blank": list(self.left_blank),
            "button": self.submit_text,
            "terms": [list(link) for link in self.terms],
            "message": self.message,
        }

    def material(self) -> dict:
        """What the owner's approval is bound to. Values are digests, so it can be saved anywhere; a change to what
        would be typed, ticked or pressed changes it."""

        def digest(item: PlannedField) -> str:
            return "" if item.secret or not item.value else hashlib.sha256(item.value.encode()).hexdigest()[:16]

        return {
            "host": self.host,
            "typed": [[item.role, item.label, item.source, digest(item)] for item in self.typed],
            "boxes": [[box.label, box.kind, box.tick] for box in self.boxes],
            "left_blank": list(self.left_blank),
            "button": self.submit_text,
            "terms": [list(link) for link in self.terms],
            "recipe": self.recipe,
        }


def profile_values(profile: Any) -> dict[str, str]:
    """The profile's own values a create-account form may ask for. The legal name goes on forms."""
    first, last = (str(profile.fact_value(key) or "").strip() for key in ("legal_first_name", "legal_last_name"))
    values = {
        "first_name": first,
        "last_name": last,
        "full_name": " ".join(part for part in (first, last) if part) if first and last else "",
        "preferred_name": str(profile.fact_value("preferred_name") or "").strip(),
        "phone": str(profile.fact_value("phone") or "").strip(),
    }
    return {key: value for key, value in values.items() if value}


def _label(item: dict) -> str:
    return _squash(_STAR.sub("", str(item.get("label") or "")).strip() or item.get("placeholder") or item.get("name") or item.get("id") or "Unnamed field", 160)


def _stopped(code: str, page: SignUpPage, **extra: Any) -> SignUpPlan:
    return SignUpPlan("stop", stop=code, host=page.host, rules=page.rules, message=page.message, **extra)


def plan_signup(description: dict, known: dict[str, str], account_email: str, answers: dict[str, str] | None = None) -> SignUpPlan:
    """What would be typed and ticked on this page's create-account form. Nothing is done here.

    `known` is `profile_values(profile)`; `account_email` is the address the account is registered with; `answers`
    holds the owner's answers to an earlier plan's questions, by field key.
    """
    answers = {str(key): str(value) for key, value in (answers or {}).items() if str(value).strip()}
    page = read_page(description, account_email=account_email)
    if page.kind != "signup_form":
        return _stopped(page.stop or "not_signup", page)
    if not secure_page(str(description.get("url") or "")):
        return _stopped("not_secure", page)
    address = (account_email or "").strip()
    if "@" not in address:
        return _stopped("no_email", page)
    try:
        recipe = recipe_for(page.rules)
    except RulesImpossible:
        return _stopped("password_rules", page)
    plan = SignUpPlan("ready", host=page.host, rules=page.rules, recipe=recipe)
    members = [item for item in _fields(description) if int(item.get("form", -1)) == page.form]
    passwords = emails = 0
    for item in members:
        kind, role, label, needed = str(item.get("type") or "").lower(), field_role(item), _label(item), is_required(item)
        key = str(item["key"])
        if role == "box":
            _plan_box(plan, item, key, label, needed, answers)
        elif role == "password":
            passwords += 1
            if passwords > 2:
                return _stopped("not_signup", page)
            plan.typed.append(PlannedField(key, "password" if passwords == 1 else "password_confirm", label, "password_manager", secret=True))
        elif role == "email" and kind in _TEXT_TYPES:
            emails += 1  # a second email box is the "type it again" box
            plan.typed.append(PlannedField(key, "email" if emails == 1 else "email_confirm", label, "account_email", address))
        elif role in ("first_name", "last_name", "full_name") and kind in _TEXT_TYPES:
            if known.get(role):
                plan.typed.append(PlannedField(key, role, label, "profile", known[role]))
            elif key in answers:
                plan.typed.append(PlannedField(key, role, label, "owner", answers[key]))
            else:
                plan.questions.append(Question(key, label, "profile_missing"))  # an account needs a name whether or not the box is starred
        elif role == "preferred_name" and kind in _TEXT_TYPES and known.get("preferred_name"):
            plan.typed.append(PlannedField(key, role, label, "profile", known["preferred_name"]))
        elif role in ("phone", "preferred_name", "middle_name") and kind in _TEXT_TYPES and not needed:
            plan.left_blank.append(label)
        elif role == "phone" and kind in _TEXT_TYPES:
            if _PHONE_VERIFY.search(f"{label} {item.get('hint') or ''}"):
                return _stopped("phone_verification", page)  # the page says it will text a code to this number
            if known.get("phone"):
                plan.typed.append(PlannedField(key, role, label, "profile", known["phone"]))
            else:
                _ask(plan, item, key, label, "profile_missing", answers)
        elif not needed and key not in answers:
            plan.left_blank.append(label)
        else:
            why = {"security_question": "security_question", "security_answer": "security_question", "birth_date": "sensitive", "username": "username"}.get(
                role, "unknown_required"
            )
            _ask(plan, item, key, label, why, answers)
    if not any(item.role == "email" for item in plan.typed) or passwords == 0:
        return _stopped("not_signup", page)
    buttons = [
        (str(b.get("key") or ""), _squash(b.get("text"), 120), str(b.get("type") or ""))
        for b in description.get("buttons") or []
        if isinstance(b, dict) and int(b.get("form", -1)) == page.form
    ]
    buttons = [button for button in buttons if not _WITH.search(button[1])]
    press = (
        next((b for b in buttons if _UP.search(b[1])), None)
        or next((b for b in buttons if b[2] == "submit" and not _IN.search(b[1])), None)
        or next((b for b in buttons if _ONWARD.search(b[1])), None)
    )
    if press is None:
        return _stopped("no_submit", page)
    plan.submit, plan.submit_text = press[0], press[1]
    links = [link for box in plan.boxes if box.tick for link in box.links]
    links += [(_squash(link.get("text"), 120), str(link.get("href") or "")) for link in description.get("links") or [] if _TERMS_LINK.search(str(link.get("text") or ""))]
    plan.terms = list(dict.fromkeys((name, href) for name, href in links if href.lower().startswith("https://") and len(href) <= 2000))[:6]
    if len(plan.questions) > MAX_QUESTIONS:
        return _stopped("too_many_questions", page, questions=plan.questions)
    plan.state = "ask" if plan.questions else "ready"
    return plan


def _ask(plan: SignUpPlan, item: dict, key: str, label: str, why: str, answers: dict[str, str]) -> None:
    kind = str(item.get("type") or "").lower()
    options = tuple(_squash(option, 80) for option in item.get("options") or [] if _squash(option))[:MAX_OPTIONS]
    secret = field_role(item) == "security_answer"
    if key in answers and (not options or answers[key] in options):
        plan.typed.append(PlannedField(key, field_role(item), label, "owner", answers[key], secret=secret))
        return
    plan.questions.append(Question(key, label, why, "choice" if kind in ("select", "radio") else "text", options, secret))


def _plan_box(plan: SignUpPlan, item: dict, key: str, label: str, needed: bool, answers: dict[str, str]) -> None:
    kind = box_kind(item)
    links = tuple((_squash(link.get("text"), 120), str(link.get("href") or "")) for link in item.get("links") or [] if isinstance(link, dict))
    said_yes = answers.get(key, "").strip().lower() in _YES
    if kind == "consent":
        tick = True
    elif kind in ("marketing", "remember") and not needed:
        tick = False
    else:  # a box that joins consent with marketing, a required marketing box, or a required box of another kind
        tick = said_yes and needed
        if needed and not said_yes:
            plan.questions.append(Question(key, label, "bundled_consent" if kind in ("bundled", "marketing") else "required_box", "box"))
    plan.boxes.append(PlannedBox(key, label, kind, tick, needed, links))
