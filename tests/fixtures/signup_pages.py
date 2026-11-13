"""Create-account pages for the sign-up recognition tests. Every site, name and address is fictional.

Two forms of the same thing: page descriptions (the shape `apply.signup.PAGE_JS` returns) built with `field` and
`page`, and a few whole HTML pages that the browser test reads with `PAGE_JS` itself."""

from __future__ import annotations

SITE = "careers.northwindtraders.test"
URL = f"https://{SITE}/account/new"
TERMS = f"https://{SITE}/legal/terms"
PRIVACY = f"https://{SITE}/legal/privacy"
RULES_HINT = "At least 10 characters with an uppercase letter, a lowercase letter and a number."


def field(key: str, type: str = "text", label: str = "", **extra) -> dict:
    base = {
        "key": key,
        "type": type,
        "form": 0,
        "name": "",
        "id": "",
        "autocomplete": "",
        "label": label,
        "placeholder": "",
        "required": False,
        "minlength": 0,
        "maxlength": 0,
        "pattern": "",
        "hint": "",
        "invalid": False,
        "checked": False,
        "filled": False,
        "options": [],
        "links": [],
    }
    return {**base, **extra}


def button(key: str, text: str, form: int = 0, type: str = "submit") -> dict:
    return {"key": key, "form": form, "type": type, "text": text}


def link(key: str, text: str, href: str) -> dict:
    return {"key": key, "text": text, "href": href}


def page(fields=(), buttons=(), links=(), text: str = "", url: str = URL, alerts=(), captcha: str = "", title: str = "Create your account") -> dict:
    words = [
        text,
        *(str(item.get("label") or "") for item in fields),
        *(str(item.get("hint") or "") for item in fields),
        *(b["text"] for b in buttons),
        *(item["text"] for item in links),
        *alerts,
    ]
    return {
        "url": url,
        "title": title,
        "text": "\n".join(word for word in words if word),
        "fields": list(fields),
        "buttons": list(buttons),
        "links": list(links),
        "alerts": list(alerts),
        "captcha": captcha,
    }


def consent_box(key: str = "k7", required: bool = True) -> dict:
    return field(
        key,
        "checkbox",
        "I agree to the Terms of Use and the Privacy Policy",
        required=required,
        links=[{"text": "Terms of Use", "href": TERMS}, {"text": "Privacy Policy", "href": PRIVACY}],
    )


def signup_fields() -> list[dict]:
    return [
        field("k1", label="First name *", autocomplete="given-name", required=True),
        field("k2", label="Last name *", autocomplete="family-name", required=True),
        field("k3", "email", "Email address *", autocomplete="email", required=True),
        field("k4", "password", "Password *", autocomplete="new-password", required=True, minlength=10, hint=RULES_HINT),
        field("k5", "password", "Confirm password *", autocomplete="new-password", required=True),
        field("k6", "tel", "Mobile number (optional)", autocomplete="tel"),
        consent_box("k7"),
        field("k8", "checkbox", "Send me job alerts and news about roles like this", checked=True),
        field("k9", "checkbox", "Remember me on this computer"),
    ]


def signup_page(fields=None, **extra) -> dict:
    buttons = extra.pop("buttons", [button("k10", "Create account")])
    links = extra.pop("links", [link("k11", "Already have an account? Sign in", f"https://{SITE}/login"), link("k12", "Privacy Policy", PRIVACY)])
    return page(signup_fields() if fields is None else fields, buttons, links, **extra)


def signin_page(create_href: str = URL) -> dict:
    fields = [field("k1", "email", "Email", autocomplete="username"), field("k2", "password", "Password", autocomplete="current-password")]
    return page(
        fields,
        [button("k3", "Sign in")],
        [link("k4", "Forgot your password?", f"https://{SITE}/reset"), link("k5", "Create an account", create_href)],
        url=f"https://{SITE}/login",
        title="Sign in",
    )


def code_page(text: str, split: bool = False, **extra) -> dict:
    boxes = (
        [field(f"k{n}", label=f"Digit {n}", maxlength=1, autocomplete="one-time-code" if n == 1 else "") for n in range(1, 7)]
        if split
        else [field("k1", label="Verification code", autocomplete="one-time-code")]
    )
    return page(
        boxes,
        [button("k9", "Verify")],
        [link("k10", "Send a new code", f"https://{SITE}/account/verify?again=1")],
        text=text,
        url=f"https://{SITE}/account/verify",
        title="Verify",
        **extra,
    )


SIGNUP_HTML = f"""<!doctype html><html><head><title>Create your account</title></head><body>
<header><form action="/search"><input type="search" name="q" aria-label="Search jobs"><button>Search</button></form></header>
<h1>Create your account</h1>
<form action="/account/new" method="post">
  <input type="hidden" name="csrf" value="fixture">
  <div style="display:none"><label for="website">Website</label><input id="website" name="website" required></div>
  <label for="fn">First name *</label><input id="fn" name="firstName" autocomplete="given-name" required>
  <label for="ln">Last name *</label><input id="ln" name="lastName" autocomplete="family-name" required>
  <label for="em">Email address *</label><input id="em" type="email" name="email" autocomplete="email" required>
  <label for="pw">Password *</label>
  <input id="pw" type="password" name="password" autocomplete="new-password" minlength="10" aria-describedby="pwhint" required>
  <p id="pwhint">{RULES_HINT}</p>
  <label for="pw2">Confirm password *</label><input id="pw2" type="password" name="password2" autocomplete="new-password" required>
  <label for="ph">Mobile number (optional)</label><input id="ph" type="tel" name="phone" autocomplete="tel">
  <label for="src">How did you hear about us?</label>
  <select id="src" name="source"><option value="">Choose one</option><option value="a">A friend</option><option value="b">A job board</option></select>
  <fieldset><legend>Contact me about this role by</legend>
    <label><input type="radio" name="contact" value="mail"> Email</label><label><input type="radio" name="contact" value="call"> A call</label></fieldset>
  <label><input type="checkbox" name="terms" required> I agree to the <a href="{TERMS}">Terms of Use</a> and the <a href="{PRIVACY}">Privacy Policy</a></label>
  <label><input type="checkbox" name="alerts" checked> Send me job alerts and news about roles like this</label>
  <button type="submit">Create account</button>
</form>
<p>Already have an account? <a href="/login">Sign in</a></p>
<footer><form action="/newsletter"><label for="nl">Your email</label><input id="nl" type="email" name="nl"><button>Subscribe</button></form></footer>
</body></html>"""

CODE_HTML = """<!doctype html><html><head><title>Verify your email</title></head><body>
<h1>Check your email</h1>
<p>We emailed a 6-digit code to a***@example.test. Enter the code to finish creating your account.</p>
<p>The email comes from no-reply@mail.hiring-desk.test.</p>
<form action="/account/verify" method="post">
  <input aria-label="Digit 1" maxlength="1" inputmode="numeric" autocomplete="one-time-code"><input aria-label="Digit 2" maxlength="1" inputmode="numeric">
  <input aria-label="Digit 3" maxlength="1" inputmode="numeric"><input aria-label="Digit 4" maxlength="1" inputmode="numeric">
  <input aria-label="Digit 5" maxlength="1" inputmode="numeric"><input aria-label="Digit 6" maxlength="1" inputmode="numeric">
  <button type="submit">Verify</button>
</form>
</body></html>"""

REJECTED_HTML = SIGNUP_HTML.replace(
    f'<p id="pwhint">{RULES_HINT}</p>',
    f'<p id="pwhint">{RULES_HINT}</p><p role="alert">Your password must include a special character and be no more than 24 characters.</p>',
)

WIDGET_HTML = SIGNUP_HTML.replace(
    '<button type="submit">Create account</button>', '<div data-captcha-widget style="width:300px;height:70px">I am not a robot</div><button type="submit">Create account</button>'
)
