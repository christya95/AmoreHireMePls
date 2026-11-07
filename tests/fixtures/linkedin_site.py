"""Local stand-in for a LinkedIn job page. No live LinkedIn, no messages, no connection requests.

A signed-in page opens a two-step form, then a review page whose only send button is Submit application.
A signed-out page and a verify-it's-you page have no form and record nothing.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

router = APIRouter()
LI_SUBMISSIONS: list[dict] = []
COMPANY_SUBMISSIONS: list[dict] = []
OAUTH_HITS: list[dict] = []

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


_LOOPBACK = re.compile(r"^127(?:\.\d{1,3}){3}(?::\d{2,5})?$")
# Names that resolve to loopback. The gate sees the name, not 127.0.0.1, so a script here is a different host.
_NIP = re.compile(r"^(?:[a-z0-9-]+\.)*127\.0\.0\.1\.nip\.io(?::\d{2,5})?$")


def _fixture_host(value: str) -> str:
    text = (value or "").strip().lower()
    return text if _LOOPBACK.fullmatch(text) or _NIP.fullmatch(text) else ""


@router.get("/jobs/view/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str, request: Request, variant: str = "", company: str = "", asset: str = ""):
    if variant == "login":
        return RedirectResponse("/li/login", status_code=303)
    if variant == "checkpoint":
        return RedirectResponse("/li/checkpoint", status_code=303)
    if variant == "external":
        return HTMLResponse(_offsite_page(job_id, f"/company/{job_id}"))
    if variant == "neither":
        return HTMLResponse(_offsite_page(job_id, f"/company/{job_id}?kind=neither"))
    if variant == "own":
        return HTMLResponse(_offsite_page(job_id, f"/company/{job_id}?kind=own"))
    if variant == "assets":
        port = request.url.port
        company_host = _fixture_host(company) or "site.127.0.0.1.nip.io"
        asset_host = _fixture_host(asset) or "cdn.127.0.0.1.nip.io"
        dest = f"http://{company_host}:{port}/company/{job_id}?kind=assets&asset={asset_host}:{port}"
        return HTMLResponse(_offsite_page(job_id, "/safety/go/?next=" + quote(dest, safe="")))
    if variant == "popup":
        port = request.url.port
        dest = f"http://site.127.0.0.1.nip.io:{port}/company/{job_id}?kind=popup&job_host=127.0.0.1:{port}"
        return HTMLResponse(_offsite_page(job_id, "/safety/go/?next=" + quote(dest, safe="")))
    if variant == "blocked":
        return RedirectResponse("/uas/login", status_code=303)
    return HTMLResponse(_form(job_id))


@router.get("/safety/go/")
def safety_go(next: str = ""):
    """The apply link leaves this host the way a job site redirects off the posting. Loopback only."""
    parts = urlsplit(next)
    if parts.scheme != "http" or not _fixture_host(parts.hostname or ""):
        return HTMLResponse("no", status_code=400)
    return RedirectResponse(next, status_code=303)


def _offsite_page(job_id: str, href: str) -> str:
    return f'<html><head><title>Example role</title></head><body><p>This posting opens another site.</p><a href="{href}" aria-label="Apply">Apply</a></body></html>'


def _company_form(job_id: str) -> str:
    """The same contact form, opened by an apply-with-LinkedIn control. A second control is the page's own apply."""
    html = _form(job_id)
    html = html.replace(
        '<button type="button" id="easy-apply" aria-label="Easy Apply">Easy Apply</button>',
        '<button type="button" id="apply-now" aria-label="Apply now">Apply now</button>'
        '<button type="button" id="apply-with-linkedin" aria-label="Apply With LinkedIn">Apply With LinkedIn</button>',
    )
    html = html.replace('enctype="multipart/form-data">', 'enctype="multipart/form-data"><input type="hidden" id="opened-by" name="opened_by" value="">')
    html = html.replace(
        "document.getElementById('easy-apply').addEventListener('click', () => {",
        "document.getElementById('apply-with-linkedin').addEventListener('click', () => {\n  document.getElementById('opened-by').value = 'apply-with-linkedin';",
    )
    return html


def _asset_company_page(job_id: str, script_src: str) -> str:
    """Both apply controls are on the page. The form stays hidden until a script from another host opens it."""
    html = _form(job_id)
    html = html.replace(
        '<button type="button" id="easy-apply" aria-label="Easy Apply">Easy Apply</button>',
        '<button type="button" id="apply-now" aria-label="Apply now">Apply now</button>'
        '<button type="button" id="apply-with-linkedin" aria-label="Apply With LinkedIn">Apply With LinkedIn</button>',
    )
    html = html.replace('enctype="multipart/form-data">', 'enctype="multipart/form-data"><input type="hidden" id="opened-by" name="opened_by" value="">')
    html = html.replace(
        "document.getElementById('easy-apply').addEventListener('click', () => {\n  document.getElementById('modal').hidden = false;\n  show('1');\n});\n",
        "",
    )
    if script_src:
        html = html.replace("</body>", f'<script src="{script_src}"></script></body>')
    return html


_OPEN_FORM_JS = """
document.getElementById('apply-with-linkedin').addEventListener('click', () => {
  const opened = document.getElementById('opened-by');
  if (opened) opened.value = 'apply-with-linkedin';
  document.getElementById('modal').hidden = false;
  show('1');
});
"""


@router.get("/assets/open-form.js")
def open_form_js():
    return Response(_OPEN_FORM_JS, media_type="application/javascript")


def _own_apply_page(job_id: str) -> str:
    return f"""<html><head><title>Example role</title></head><body>
<button type="button" id="apply-now" aria-label="Apply now">Apply now</button>
<form id="own-form" method="post" action="/company/{job_id}/submit" hidden>
<label for="email">Email address</label><input id="email" name="email" required>
<label for="internal">Internal code</label><input id="internal" name="internal" required>
<button type="submit" aria-label="Submit application">Submit application</button>
</form>
<script>
document.getElementById('apply-now').addEventListener('click', () => {{
  document.getElementById('own-form').hidden = false;
}});
</script></body></html>"""


def _popup_company_page(job_id: str, job_host: str) -> str:
    """Apply with LinkedIn opens a popup on the session host. A cross-site open is not the saved session."""
    host = _fixture_host(job_host)
    target = f"http://{host}/oauth/web-pkce/authorization?job={job_id}" if host else ""
    return (
        "<html><head><title>Example role</title></head><body>"
        '<button type="button" id="apply-with-linkedin" aria-label="Apply With LinkedIn">Apply With LinkedIn</button>'
        "<script>document.getElementById('apply-with-linkedin').addEventListener('click', () => {"
        f"window.open({target!r}, 'awli');"
        "});</script></body></html>"
    )


@router.get("/company/{job_id}", response_class=HTMLResponse)
def company_page(job_id: str, kind: str = "", asset: str = "", job_host: str = ""):
    if kind == "neither":
        return HTMLResponse("<html><head><title>Example role</title></head><body><p>No apply control on this page.</p></body></html>")
    if kind == "own":
        return HTMLResponse(_own_apply_page(job_id))
    if kind == "assets":
        host = _fixture_host(asset)
        src = f"http://{host}/assets/open-form.js" if host else ""
        return HTMLResponse(_asset_company_page(job_id, src))
    if kind == "popup":
        return HTMLResponse(_popup_company_page(job_id, job_host))
    return HTMLResponse(_company_form(job_id))


@router.post("/company/{job_id}/submit")
async def company_submit(job_id: str, email: str = Form(""), internal: str = Form("")):
    COMPANY_SUBMISSIONS.append({"job_id": job_id, "email": email, "internal": internal})
    return RedirectResponse(f"/jobs/view/{job_id}/confirmation", status_code=303)


@router.get("/oauth/web-pkce/authorization")
def oauth_start(request: Request, job: str = ""):
    """A cross-site popup is signed out. A page the app opens itself keeps the saved session cookie."""
    site = (request.headers.get("sec-fetch-site") or "").lower()
    cookie = request.cookies.get("awli") == "1"
    ok = cookie and site in {"none", "same-origin"}
    OAUTH_HITS.append({"site": site, "cookie": cookie, "ok": ok})
    job_id = job if re.fullmatch(r"\d{6,}", job) else JOB_ID
    if ok:
        return RedirectResponse(f"/jobs/view/{job_id}/ready", status_code=303)
    return RedirectResponse("/uas/login", status_code=303)


@router.get("/jobs/view/{job_id}/ready", response_class=HTMLResponse)
def ready_form(job_id: str):
    return HTMLResponse(_form(job_id).replace('<div id="modal" hidden>', '<div id="modal">', 1))


@router.get("/uas/login", response_class=HTMLResponse)
def blocked_login():
    return HTMLResponse(
        "<html><head><title>Sorry, you have been blocked</title></head><body>"
        "<h1>Sorry, you have been blocked</h1>"
        "<p>You are unable to access this site. This website is using a security service to protect itself.</p>"
        "</body></html>"
    )


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
    opened_by: str = Form(""),
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
            "opened_by": opened_by,
            "resume": {"filename": resume.filename if resume is not None else "", "bytes": len(data)},
        }
    )
    return RedirectResponse(f"/jobs/view/{job_id}/confirmation", status_code=303)


@router.get("/jobs/view/{job_id}/confirmation", response_class=HTMLResponse)
def confirmation(job_id: str):
    return HTMLResponse(f"<html><head><title>Application sent</title></head><body><p>Your application was sent for job {job_id}.</p></body></html>")
