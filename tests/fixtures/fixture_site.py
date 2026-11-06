"""Local ATS-like fixture site: multi-step form, upload, validation errors, receipt, slow/uncertain submit,
login wall. Runs with uvicorn in a background thread for browser tests."""

from __future__ import annotations

import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from tests.fixtures.ashby_site import router as ashby_router
from tests.fixtures.greenhouse_site import router as greenhouse_router
from tests.fixtures.lever_site import router as lever_router
from tests.fixtures.linkedin_site import router as linkedin_router

app = FastAPI()
app.include_router(greenhouse_router)  # Greenhouse style board (see greenhouse_site.py)
app.include_router(lever_router)  # Lever style board (see lever_site.py)
app.include_router(ashby_router)  # Ashby style board (see ashby_site.py)
app.include_router(linkedin_router)  # LinkedIn style job page (see linkedin_site.py)
SESSIONS: dict[str, dict] = {}
SUBMISSIONS: list[dict] = []


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>")


@app.get("/apply/{req}", response_class=HTMLResponse)
def step1(req: str, slow: int = 0):
    sid = uuid.uuid4().hex[:8]
    SESSIONS[sid] = {"req": req, "slow": slow}
    return _page(
        "Apply: step 1",
        f"""
    <form method="post" action="/apply/{req}/step1/{sid}">
      <label>First name <input name="first_name" id="first_name"></label>
      <label>Last name <input name="last_name" id="last_name"></label>
      <label>Email <input name="email" id="email"></label>
      <label>Phone <input name="phone" id="phone"></label>
      <button type="submit" id="next1">Next</button>
    </form>""",
    )


@app.post("/apply/{req}/step1/{sid}", response_class=HTMLResponse)
def post1(req: str, sid: str, first_name: str = Form(""), last_name: str = Form(""), email: str = Form(""), phone: str = Form("")):
    if not phone:
        return _page("Apply: step 1", '<div class="error" role="alert">Phone is required</div>')
    SESSIONS[sid].update(first_name=first_name, last_name=last_name, email=email, phone=phone)
    return RedirectResponse(f"/apply/{req}/step2/{sid}", status_code=303)


@app.get("/apply/{req}/step2/{sid}", response_class=HTMLResponse)
def step2(req: str, sid: str):
    return _page(
        "Apply: step 2",
        f"""
    <form method="post" action="/apply/{req}/step2/{sid}" enctype="multipart/form-data">
      <label>Resume <input type="file" name="resume" id="resume"></label>
      <label>Work authorization <select name="work_auth" id="work_auth"><option value="">Select</option><option value="yes">Authorized, no sponsorship</option><option value="no">Needs sponsorship</option></select></label>
      <label>Desired salary <input name="salary" id="salary"></label>
      <button type="submit" id="next2">Review</button>
    </form>""",
    )


@app.post("/apply/{req}/step2/{sid}", response_class=HTMLResponse)
async def post2(req: str, sid: str, resume: UploadFile = File(None), work_auth: str = Form(""), salary: str = Form("")):
    if resume is None or not resume.filename:
        return _page("Apply: step 2", '<div class="error" role="alert">Resume is required</div>')
    data = await resume.read()
    SESSIONS[sid].update(resume_name=resume.filename, resume_bytes=len(data), work_auth=work_auth, salary=salary)
    return RedirectResponse(f"/apply/{req}/review/{sid}", status_code=303)


@app.get("/apply/{req}/review/{sid}", response_class=HTMLResponse)
def review(req: str, sid: str, slow: int = 0):
    s = SESSIONS[sid]
    return _page(
        "Review your application",
        f"""
    <p id="rv_marker">Review summary for requisition {req}: check every answer before you submit.</p>
    <label>Name <input id="rv_name" value="{s.get("first_name", "")} {s.get("last_name", "")}" readonly></label>
    <label>Email <input id="rv_email" value="{s.get("email", "")}" readonly></label>
    <label>Phone <input id="rv_phone" value="{s.get("phone", "")}" readonly></label>
    <label>Resume <input id="rv_resume" value="{s.get("resume_name", "")}" readonly></label>
    <label>Work authorization <input id="rv_work_auth" value="{s.get("work_auth", "")}" readonly></label>
    <label>Desired salary <input id="rv_salary" value="{s.get("salary", "")}" readonly></label>
    <form method="post" action="/apply/{req}/submit/{sid}?slow={slow}">
      <button type="submit" id="submit">Submit application</button>
    </form>""",
    )


@app.post("/apply/{req}/submit/{sid}", response_class=HTMLResponse)
def submit(req: str, sid: str, slow: int = 0):
    s = SESSIONS[sid]
    ref = "REF-" + uuid.uuid4().hex[:6].upper()
    SUBMISSIONS.append({**s, "ref": ref, "req": req})
    if slow or s.get("slow"):
        time.sleep(slow or s["slow"])
    return _page("Thank you", f"<p id='receipt'>Application received for requisition {req}. Reference {ref}.</p>")


@app.get("/login", response_class=HTMLResponse)
def login():
    return _page("Sign in", '<form><input name="user"><input type="password" name="pw"><button>Sign in</button></form>')


@app.get("/gated/{req}", response_class=HTMLResponse)
def gated(req: str):
    return RedirectResponse("/login", status_code=303)


class FixtureServer:
    def __init__(self, port: int = 8765) -> None:
        self.port = port
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fixture server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)
