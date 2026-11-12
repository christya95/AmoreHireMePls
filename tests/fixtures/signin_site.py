"""Local job site with an account wall, for the sign-in step. Every value is fictional.

One scenario per path prefix: /acct/<scenario>/job is the application form and needs a session. The scenario name
decides how the sign-in pages behave. `STATE` records what the site received, so a test can count password posts.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

USERNAME = "casey.fixture@example.com"
PASSWORD = "Fixture-Only-Pass-91x"
CODE = "424242"
NEW_CODE = "737373"
CODE_STEP = ("code", "split", "badcode", "resend", "saved")

app = FastAPI()


def _fresh() -> dict:
    return {
        "password_posts": 0,
        "passwords_ok": 0,
        "usernames": [],
        "codes": [],
        "resends": 0,
        "sessions": set(),
        "pending": set(),
        "logins_shown": 0,
        "applied": 0,
        "forgot_opened": 0,
    }


STATE: dict[str, dict] = defaultdict(_fresh)


def reset() -> None:
    STATE.clear()


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>")


def _signed_in(sc: str, request: Request) -> bool:
    return request.cookies.get(f"sid_{sc}") in STATE[sc]["sessions"]


def _session(sc: str, to: str) -> RedirectResponse:
    sid = uuid.uuid4().hex
    STATE[sc]["sessions"].add(sid)
    out = RedirectResponse(to, status_code=303)
    out.set_cookie(f"sid_{sc}", sid, httponly=True)
    return out


@app.get("/acct/{sc}/job", response_class=HTMLResponse)
def job(sc: str, request: Request):
    if sc == "blocked":
        return _page("Attention required", "<p>Sorry, you have been blocked. This site uses a security service to protect itself.</p>")
    if not _signed_in(sc, request):
        return RedirectResponse(f"/acct/{sc}/login", status_code=303)
    body = f'<form method="post" action="/acct/{sc}/apply"><label for="full_name">Full name</label><input id="full_name" name="full_name"><button id="submit">Send application</button></form>'
    return _page(f"Apply for requisition R-{sc}", body)


@app.post("/acct/{sc}/apply", response_class=HTMLResponse)
def apply(sc: str, request: Request, full_name: str = Form("")):
    if not _signed_in(sc, request):
        return RedirectResponse(f"/acct/{sc}/login", status_code=303)
    STATE[sc]["applied"] += 1
    return _page("Thank you", f"<p>Application received for requisition R-{sc}.</p>")


_FORM = (
    '<form method="post"><label>Email <input type="email" name="email" autocomplete="username"></label>'
    '<label>Password <input type="password" name="pw" autocomplete="current-password"></label><button>Sign in</button></form>'
    '<p><a href="#">Create account</a></p>'
)
_ERRORS = {
    "1": "Incorrect email or password. <script>window.pwned = 1</script>",
    "2": "Your account has been locked after too many attempts. Try again in 15 minutes.",
    "3": "Too many attempts. Please try again later.",
}


def _forgot(sc: str) -> str:
    if sc == "offsite":
        return '<p><a href="https://reset.example.org/forgot">Forgot your password?</a></p>'
    if sc == "noreset":
        return ""
    return f'<p><a href="/acct/{sc}/forgot?tenant={sc}">Forgot your password?</a></p>'


@app.get("/acct/{sc}/forgot", response_class=HTMLResponse)
def forgot(sc: str):
    STATE[sc]["forgot_opened"] += 1  # only the owner's own browser should ever come here
    return _page("Reset your password", '<form method="post"><label>Email <input type="email" name="email"></label><button>Send reset email</button></form>')


@app.get("/acct/{sc}/login", response_class=HTMLResponse)
def login(sc: str, e: str = ""):
    STATE[sc]["logins_shown"] += 1
    note = f'<p role="alert">{_ERRORS[e].replace("<", "&lt;")}</p>' if e in _ERRORS else ""
    if sc == "late":
        script = f"<div id='slot'></div><script>setTimeout(() => {{ document.getElementById('slot').innerHTML = {_FORM!r}; }}, 1500);</script>"
        return _page("Welcome back", script)
    if sc == "signup":
        body = (
            '<form method="post"><label>Email <input type="email" name="email"></label>'
            '<label>Choose a password <input type="password" name="pw" autocomplete="new-password"></label>'
            '<label>Repeat it <input type="password" name="pw2" autocomplete="new-password"></label><button>Create account</button></form>'
        )
        return _page("Create your account", body)
    if sc == "passkey":
        return _page("Sign in", "<button>Sign in with a passkey</button>")
    if sc == "captcha":
        return _page("Sign in", _FORM + '<div data-captcha-challenge="grid">Select every picture with a boat</div>')
    if sc == "userfirst":
        return _page("Sign in", '<form method="post" action="/acct/userfirst/user"><label>Email <input type="email" name="email"></label><button>Next</button></form>')
    return _page("Sign in", note + _FORM + _forgot(sc))


@app.post("/acct/{sc}/user", response_class=HTMLResponse)
def user_step(sc: str, email: str = Form("")):
    STATE[sc]["usernames"].append(email)
    body = f'<form method="post" action="/acct/{sc}/login"><input type="hidden" name="email" value="{email}"><label>Password <input type="password" name="pw"></label><button>Sign in</button></form>'
    return _page("Sign in", body)


@app.post("/acct/{sc}/login")
def login_post(sc: str, email: str = Form(""), pw: str = Form("")):
    state = STATE[sc]
    state["password_posts"] += 1
    state["usernames"].append(email)
    good = pw == PASSWORD and email == USERNAME
    state["passwords_ok"] += int(good)
    if sc == "locked":
        return RedirectResponse(f"/acct/{sc}/login?e=2", status_code=303)
    if sc == "throttled":
        return RedirectResponse(f"/acct/{sc}/login?e=3", status_code=303)
    if sc == "wrong" or not good:
        return RedirectResponse(f"/acct/{sc}/login?e=1", status_code=303)
    if sc in CODE_STEP:
        pre = uuid.uuid4().hex
        state["pending"].add(pre)
        out = RedirectResponse(f"/acct/{sc}/verify", status_code=303)
        out.set_cookie(f"pre_{sc}", pre, httponly=True)
        return out
    if sc == "checkpoint":
        return RedirectResponse(f"/acct/{sc}/checkpoint/", status_code=303)
    if sc == "hops":
        return _session(sc, f"/acct/{sc}/hop/1")
    if sc == "never":
        return RedirectResponse(f"/acct/{sc}/spin", status_code=303)
    return _session(sc, f"/acct/{sc}/job")


@app.get("/acct/{sc}/hop/{n}", response_class=HTMLResponse)
def hop(sc: str, n: int):
    target = f"/acct/{sc}/hop/{n + 1}" if n < 3 else f"/acct/{sc}/job"
    return _page("One moment", f"<p>Taking you to your account.</p><script>setTimeout(() => {{ location.href = {target!r}; }}, 700);</script>")


@app.get("/acct/{sc}/spin", response_class=HTMLResponse)
def spin(sc: str):
    # A page that never settles: it keeps showing a password box with no message.
    return _page("Sign in", '<form><label>Password <input type="password" name="pw"></label></form>')


@app.get("/acct/{sc}/checkpoint/", response_class=HTMLResponse)
def checkpoint(sc: str):
    return _page("Verify it's you", "<p>Open the app on your phone and approve this sign-in.</p>")


@app.get("/acct/{sc}/verify", response_class=HTMLResponse)
def verify(sc: str, e: str = ""):
    note = '<p role="alert">That code is incorrect or has expired.</p>' if e else ""
    if sc == "split":
        boxes = "".join(f'<input name="d{i}" maxlength="1" inputmode="numeric" aria-label="Digit {i}">' for i in range(1, 7))
        script = (
            "<script>const row = [...document.querySelectorAll('#digits input')]; row.forEach((el, i) => el.addEventListener('input', () => "
            "{ if (el.value && row[i + 1]) row[i + 1].focus(); }));</script>"
        )
        field = f'<fieldset><legend>Verification code</legend><div id="digits">{boxes}</div></fieldset>{script}'
    else:
        field = '<label>Verification code <input name="code" autocomplete="one-time-code" inputmode="numeric"></label>'
    resend = f'<form method="post" action="/acct/{sc}/resend"><button>Resend code</button></form>' if sc == "resend" else ""
    body = f'{note}<p>Enter the verification code we sent to your phone.</p><form method="post">{field}<button>Verify</button></form>{resend}'
    return _page("Two-step verification", body)


@app.post("/acct/{sc}/resend")
def resend(sc: str):
    STATE[sc]["resends"] += 1
    return RedirectResponse(f"/acct/{sc}/verify", status_code=303)


@app.post("/acct/{sc}/verify")
async def verify_post(sc: str, request: Request):
    form = await request.form()
    code = str(form.get("code") or "") or "".join(str(form.get(f"d{i}") or "") for i in range(1, 7))
    state = STATE[sc]
    state["codes"].append(code)
    if request.cookies.get(f"pre_{sc}") not in state["pending"]:
        return RedirectResponse(f"/acct/{sc}/login", status_code=303)
    wanted = NEW_CODE if state["resends"] else CODE
    if sc == "badcode" and len(state["codes"]) < 2:
        return RedirectResponse(f"/acct/{sc}/verify?e=1", status_code=303)
    if code != wanted:
        return RedirectResponse(f"/acct/{sc}/verify?e=1", status_code=303)
    return _session(sc, f"/acct/{sc}/job")


class SignInSite:
    def __init__(self, port: int) -> None:
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        reset()
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fixture server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)
