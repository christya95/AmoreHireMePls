"""Local stand-in for a LinkedIn job page. No live LinkedIn, no messages, no connection requests.

A signed-in page opens a two-step form, then a review page whose only send button is Submit application.
A signed-out page and a verify-it's-you page have no form and record nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
LI_SUBMISSIONS: list[dict] = []

JOB_ID = "1000000001"

QUESTIONS = [
    {"id": "email", "label": "Email address", "type": "text", "required": True, "step": 1},
    {"id": "phone", "label": "Mobile phone number", "type": "text", "required": True, "step": 1},
    {"id": "resume", "label": "Resume", "type": "file", "required": True, "step": 1},
    {
        "id": "years",
        "label": "How many years of experience do you have?",
        "type": "select",
        "required": True,
        "step": 2,
        "options": ["Less than 1 year", "1-2 years", "2-4 years", "5+ years"],
    },
    {"id": "work_auth", "label": "Are you authorized to work in this country?", "type": "select", "required": True, "step": 2, "options": ["Yes", "No"]},
]
REFERRAL = {"id": "referral", "label": "Referral code", "type": "text", "required": True, "step": 2, "options": []}


def questions_for(variant: str) -> list[dict]:
    rows = [dict(row) for row in QUESTIONS]
    if variant == "missing":
        rows.append(dict(REFERRAL))
    return rows


@router.get("/li-api/questions/{job_id}")
def questions(job_id: str, variant: str = ""):
    return {"id": job_id, "questions": questions_for(variant)}


def _options(name: str, values: list[str]) -> str:
    opts = "".join(f'<option value="{value}">{value}</option>' for value in values)
    return f'<label for="{name}">{name}</label><select id="{name}" name="{name}"><option value="">Select</option>{opts}</select>'


def _form(job_id: str) -> str:
    years = next(row["options"] for row in QUESTIONS if row["id"] == "years")
    auth = next(row["options"] for row in QUESTIONS if row["id"] == "work_auth")
    return f"""<html><head><title>Example role</title></head><body>
<button type="button" id="easy-apply" aria-label="Easy Apply">Easy Apply</button>
<div id="modal" hidden>
<form id="apply-form" method="post" action="/jobs/view/{job_id}/submit" enctype="multipart/form-data">
<section data-step="1">
<label for="email">Email address</label><input id="email" name="email">
<label for="phone">Mobile phone number</label><input id="phone" name="phone">
<label for="resume">Resume</label><input id="resume" name="resume" type="file">
<p id="resume-name"></p>
<button type="button" class="advance" data-show="2" aria-label="Continue to next step">Next</button>
</section>
<section data-step="2" hidden>
<label for="years">How many years of experience do you have?</label>{_options("years", years).split("</label>", 1)[-1]}
<label for="work_auth">Are you authorized to work in this country?</label>{_options("work_auth", auth).split("</label>", 1)[-1]}
<button type="button" class="advance" data-show="3" aria-label="Continue to next step">Next</button>
</section>
<section data-step="3" hidden>
<p>Review your application for job {job_id}.</p>
<button type="submit" aria-label="Submit application">Submit application</button>
</section>
</form></div>
<script>
document.getElementById('easy-apply').addEventListener('click', () => {{
  document.getElementById('modal').hidden = false;
  show('1');
}});
function show(n) {{
  document.querySelectorAll('[data-step]').forEach(s => {{ s.hidden = s.dataset.step !== n; }});
}}
document.querySelectorAll('.advance').forEach(b => b.addEventListener('click', () => show(b.dataset.show)));
const resume = document.getElementById('resume');
resume.addEventListener('change', () => {{
  const file = resume.files && resume.files[0];
  document.getElementById('resume-name').textContent = file ? file.name : '';
}});
</script></body></html>"""


@router.get("/jobs/view/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str, variant: str = ""):
    if variant == "login":
        return RedirectResponse("/li/login", status_code=303)
    if variant == "checkpoint":
        return RedirectResponse("/li/checkpoint", status_code=303)
    if variant == "external":
        return HTMLResponse(f'<html><head><title>Example role</title></head><body><p>This posting opens another site.</p><a href="/jobs/view/{job_id}">Apply</a></body></html>')
    return HTMLResponse(_form(job_id))


@router.get("/li/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(
        '<html><head><title>Sign in</title></head><body><form><label>Email <input name="user"></label><label>Password <input type="password" name="pw"></label></form></body></html>'
    )


@router.get("/li/checkpoint", response_class=HTMLResponse)
def checkpoint_page():
    return HTMLResponse("<html><head><title>Verify it's you</title></head><body><p>Verify it's you before you continue.</p></body></html>")


@router.post("/jobs/view/{job_id}/submit")
async def submit(
    job_id: str,
    email: str = Form(""),
    phone: str = Form(""),
    years: str = Form(""),
    work_auth: str = Form(""),
    resume: UploadFile | None = File(None),
):
    data = await resume.read() if resume is not None and resume.filename else b""
    LI_SUBMISSIONS.append(
        {
            "job_id": job_id,
            "email": email,
            "phone": phone,
            "years": years,
            "work_auth": work_auth,
            "resume": {"filename": resume.filename if resume is not None else "", "bytes": len(data)},
        }
    )
    return RedirectResponse(f"/jobs/view/{job_id}/confirmation", status_code=303)


@router.get("/jobs/view/{job_id}/confirmation", response_class=HTMLResponse)
def confirmation(job_id: str):
    return HTMLResponse(f"<html><head><title>Application sent</title></head><body><p>Your application was sent for job {job_id}.</p></body></html>")
