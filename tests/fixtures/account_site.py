"""Local create-account site. Every value is fictional. Nothing here is a real employer or a real mailbox.

`/join/<scenario>/job` is the application form. Without a session it redirects to the create-account page. The
scenario decides what that page does. `STATE` counts form posts so a test can prove the form was sent once.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

EMAIL = "casey.fixture@example.test"
CODE = "424242"

app = FastAPI()


def _fresh() -> dict:
    return {"posts": 0, "passwords": [], "codes": [], "resends": 0, "sessions": set(), "applied": 0}


STATE: dict[str, dict] = defaultdict(_fresh)


def reset() -> None:
    STATE.clear()


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>")


def _form(sc: str, alert: str = "") -> str:
    note = f'<div role="alert">{alert}</div>' if alert else ""
    extra = ""
    if sc == "security":
        extra = '<label>Security question <input name="secret" required></label>'
    if sc == "captcha":
        extra += '<div data-captcha-widget style="display:block;width:220px;height:48px">Verify you are human</div>'
    return f"""{note}<form method="post" action="/join/{sc}/create">
      <label>Email <input type="email" name="email" autocomplete="email" required></label>
      <label>Password <input type="password" name="password" autocomplete="new-password" required></label>
      <label>Confirm password <input type="password" name="confirm" autocomplete="new-password" required></label>
      <label><input type="checkbox" name="terms" required> I agree to the <a href="https://jobs.example.test/terms">terms</a> and <a href="https://jobs.example.test/privacy">privacy policy</a></label>
      <label><input type="checkbox" name="marketing"> Send me marketing email</label>
      {extra}
      <button type="submit">Create account</button>
    </form>"""


def _signed(sc: str, request: Request) -> bool:
    return request.cookies.get(f"sid_{sc}") in STATE[sc]["sessions"]


def _session(sc: str, to: str) -> RedirectResponse:
    sid = uuid.uuid4().hex
    STATE[sc]["sessions"].add(sid)
    out = RedirectResponse(to, status_code=303)
    out.set_cookie(f"sid_{sc}", sid, httponly=True)
    return out


@app.get("/join/{sc}/job", response_class=HTMLResponse)
def job(sc: str, request: Request):
    if not _signed(sc, request):
        return RedirectResponse(f"/join/{sc}/signup", status_code=303)
    body = f'<form method="post" action="/join/{sc}/apply"><label for="full_name">Full name</label><input id="full_name" name="full_name"><button id="submit">Send application</button></form>'
    return _page(f"Apply for requisition R-{sc}", body)


@app.get("/join/{sc}/signup", response_class=HTMLResponse)
def signup(sc: str):
    if sc == "phone":
        return _page("Sign in", "<p>We sent a code by text message to your phone.</p><button>Create account</button>")
    return _page("Create your account", _form(sc))


@app.post("/join/{sc}/create")
def create(sc: str, password: str = Form("")):
    state = STATE[sc]
    state["posts"] += 1
    state["passwords"].append(len(password))
    if sc == "exists":
        return _page("Create your account", '<p role="alert">This email already has an account.</p><p><a href="/join/exists/reset">Reset your password</a></p>')
    if sc == "weak" and state["posts"] == 1:
        return _page("Create your account", _form(sc, "The password does not meet the requirements."))
    if sc == "blank":
        return HTMLResponse("<html><body></body></html>")
    if sc == "code":
        return _page(
            "Check your email",
            '<p>We emailed you a verification code.</p><form method="post" action="/join/code/code"><label>Code <input name="code" autocomplete="one-time-code"></label><button>Continue</button></form><form method="post" action="/join/code/resend"><button>Resend the code</button></form>',
        )
    if sc == "link":
        return _page("Check your email", "<p>We emailed you a link. Check your email to verify your account.</p>")
    return _session(sc, f"/join/{sc}/job")


@app.post("/join/code/code")
def code(code: str = Form("")):
    STATE["code"]["codes"].append(code)
    if code != CODE:
        return _page(
            "Check your email",
            '<p>We emailed you a verification code.</p><form method="post" action="/join/code/code"><label>Code <input name="code" autocomplete="one-time-code"></label><button>Continue</button></form>',
        )
    return _session("code", "/join/code/job")


@app.post("/join/code/resend")
def resend():
    STATE["code"]["resends"] += 1
    return _page(
        "Check your email",
        '<p>We emailed you a verification code.</p><form method="post" action="/join/code/code"><label>Code <input name="code" autocomplete="one-time-code"></label><button>Continue</button></form>',
    )


@app.get("/join/link/open", response_class=HTMLResponse)
def open_link():
    return _session("link", "/join/link/job")


@app.get("/join/{sc}/reset", response_class=HTMLResponse)
def reset_page(sc: str):
    return _page("Reset your password", "<p>Choose a new password.</p>")


@app.post("/join/{sc}/apply", response_class=HTMLResponse)
def apply(sc: str, request: Request, full_name: str = Form("")):
    if not _signed(sc, request):
        return RedirectResponse(f"/join/{sc}/signup", status_code=303)
    STATE[sc]["applied"] += 1
    return _page("Thank you", f"<p>Application received for requisition R-{sc}.</p>")


class AccountSite:
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
