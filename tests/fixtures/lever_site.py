"""Fictional Lever board for tests.

  GET  /lv-api/v0/postings/{org}/{id}     postings API. `?bare=1` omits custom questions.
  GET  /lv/{org}/{id}?variant=new|check|slow|invalid|login|extra
  POST /lv/{org}/{id}/apply               records the application, then redirects to /thanks

Nothing here is a real employer. The posting id is a fixed UUID so receipt checks can require it.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
LV_SUBMISSIONS: list[dict] = []
POSTING_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

QUESTIONS = [
    {"id": "q_why", "text": "Why do you want to work at Acme?", "type": "textarea", "required": True},
    {
        "id": "q_years",
        "text": "Years of experience",
        "type": "dropdown",
        "required": True,
        "options": [{"text": "0-1 years"}, {"text": "2-4 years"}, {"text": "5+ years"}],
    },
    {
        "id": "q_gender",
        "text": "Gender",
        "type": "dropdown",
        "required": True,
        "options": [{"text": "Woman"}, {"text": "Man"}, {"text": "I don't wish to answer"}],
    },
    {
        "id": "q_hear",
        "text": "How did you hear about this job?",
        "type": "multiple-select",
        "required": False,
        "options": [{"text": "Job board"}, {"text": "Other"}],
    },
]


def posting(org: str, posting_id: str, bare: bool, base_hint: str = "") -> dict:
    hosted = f"/lv/{org}/{posting_id}"
    body = {
        "id": posting_id,
        "text": "Backend Developer",
        "categories": {"commitment": "Permanent", "location": "Toronto", "team": "Engineering"},
        "hostedUrl": hosted,
        "applyUrl": hosted,
    }
    if not bare:
        body["customQuestions"] = QUESTIONS
    else:
        body["hostedUrl"] = hosted + "?variant=extra"
        body["applyUrl"] = hosted + "?variant=extra"
    if base_hint:
        body["hostedUrl"] = base_hint + body["hostedUrl"]
        body["applyUrl"] = base_hint + body["applyUrl"]
    return body


def _form(org: str, posting_id: str, variant: str) -> str:
    if variant == "login":
        return "<html><head><title>Sign in</title></head><body><form><label>Password <input type='password' name='pw'></label></form></body></html>"
    extra = ""
    if variant == "extra":
        extra = '<div><label for="portfolio">Portfolio URL</label><input id="portfolio" name="portfolio" required></div>'
    captcha = ""
    script = ""
    if variant == "check":
        captcha = '<div data-captcha-challenge="text" hidden><p>Type the characters you see</p><input aria-label="Characters"></div>'
        script = """<script>
          const form = document.getElementById('application-form');
          form.addEventListener('submit', (e) => {
            if (!form.dataset.verified) { e.preventDefault(); document.querySelector('[data-captcha-challenge]').hidden = false; }
          });
        </script>"""
    years = "".join(f"<option>{o}</option>" for o in ("0-1 years", "2-4 years", "5+ years"))
    gender = "".join(f"<option>{o}</option>" for o in ("Woman", "Man", "I don't wish to answer"))
    return f"""<html><head><title>Backend Developer at Acme</title></head><body>
<form id="application-form" method="post" action="/lv/{org}/{posting_id}/apply?variant={variant}" enctype="multipart/form-data">
  <div><label for="name">Full name</label><input id="name" name="name" required></div>
  <div><label for="email">Email</label><input id="email" name="email" required></div>
  <div><label for="phone">Phone</label><input id="phone" name="phone" required></div>
  <div><label for="resume">Resume</label><input id="resume" name="resume" type="file" required></div>
  <div><label for="q_why">Why do you want to work at Acme?</label><textarea id="q_why" name="q_why" required></textarea></div>
  <div><label for="q_years">Years of experience</label><select id="q_years" name="q_years" required><option value="">Select</option>{years}</select></div>
  <div><label for="q_gender">Gender</label><select id="q_gender" name="q_gender" required><option value="">Select</option>{gender}</select></div>
  <fieldset><legend>How did you hear about this job?</legend>
    <label><input type="checkbox" name="q_hear" value="Job board"> Job board</label>
    <label><input type="checkbox" name="q_hear" value="Other"> Other</label>
  </fieldset>
  {extra}{captcha}
  <button type="submit">Submit application</button>
</form>{script}</body></html>"""


@router.get("/lv-api/v0/postings/{org}/{posting_id}")
def lv_posting(org: str, posting_id: str, request: Request, bare: int = 0):
    base = str(request.base_url).rstrip("/")
    return posting(org, posting_id, bare=bool(bare), base_hint=base)


@router.get("/lv/{org}/{posting_id}", response_class=HTMLResponse)
def lv_job(org: str, posting_id: str, variant: str = "new"):
    return HTMLResponse(_form(org, posting_id, variant))


@router.post("/lv/{org}/{posting_id}/apply")
async def lv_apply(org: str, posting_id: str, request: Request, variant: str = "new"):
    form = await request.form()
    data: dict = {}
    for key in form.keys():
        vals = form.getlist(key)
        if hasattr(vals[0], "filename"):
            data[key] = {"filename": vals[0].filename, "bytes": len(await vals[0].read())}
        else:
            data[key] = vals if len(vals) > 1 else vals[0]
    if variant == "invalid":
        return HTMLResponse(_form(org, posting_id, "invalid") + '<div class="field-error" role="alert">Phone must include an area code</div>', status_code=422)
    if not data.get("name") or not isinstance(data.get("resume"), dict):
        return HTMLResponse('<div class="field-error" role="alert">This field is required</div>', status_code=422)
    LV_SUBMISSIONS.append({"org": org, "posting_id": posting_id, "variant": variant, **data})
    if variant == "slow":
        time.sleep(0.2)
        return HTMLResponse(_form(org, posting_id, "slow"))
    return RedirectResponse(f"/lv/{org}/{posting_id}/thanks", status_code=303)


@router.get("/lv/{org}/{posting_id}/thanks", response_class=HTMLResponse)
def lv_thanks(org: str, posting_id: str):
    return HTMLResponse(
        f"<html><head><title>Application submitted</title></head><body><h1>Application submitted</h1><p>Thanks for applying to {org}. Reference {posting_id}.</p></body></html>"
    )
