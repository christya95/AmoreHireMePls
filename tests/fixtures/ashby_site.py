"""Fictional Ashby board for tests.

  POST /ab-api/non-user-graphql     the public ApiJobPosting read (never an application mutation)
  GET  /ab/{org}/{id}?variant=new|check|slow|invalid|login|extra
  POST /ab/{org}/{id}/apply

Selects are React-style comboboxes: type, then click a role=option. Nothing here is a real employer.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

router = APIRouter()
AB_SUBMISSIONS: list[dict] = []
AB_OPERATIONS: list[str] = []
POSTING_ID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def _posting(posting_id: str) -> dict:
    def entry(path: str, title: str, ashby_type: str, required: bool = False, options: list[str] | None = None) -> dict:
        values = [{"label": o, "value": o} for o in options or []]
        return {"isRequired": required, "field": {"path": path, "title": title, "type": ashby_type, "isRequired": required, "selectableValues": values}}

    return {
        "id": posting_id,
        "title": "Backend Developer",
        "applicationForm": {
            "sections": [
                {
                    "fieldEntries": [
                        entry("_systemfield_name", "Name", "String", True),
                        entry("_systemfield_email", "Email", "Email", True),
                        entry("_systemfield_phone", "Phone", "Phone", True),
                        entry("_systemfield_resume", "Resume", "File", True),
                        entry("location", "Location", "Location", False),
                        entry("q_why", "Why do you want to work at Acme?", "LongText", True),
                        entry("q_years", "Years of experience", "ValueSelect", True, ["0-1 years", "2-4 years", "5+ years"]),
                        entry("q_hear", "How did you hear about this job?", "MultiValueSelect", False, ["Job board", "Other"]),
                    ]
                }
            ]
        },
        "surveyForms": [
            {
                "surveyType": "Diversity",
                "formDefinition": {"sections": [{"fields": [entry("gender", "Gender", "ValueSelect", True, ["Woman", "Man", "I don't wish to answer"])]}]},
            }
        ],
    }


def _form(org: str, posting_id: str, variant: str) -> str:
    if variant == "login":
        return "<html><head><title>Sign in</title></head><body><form><label>Password <input type='password' name='pw'></label></form></body></html>"
    extra = '<div><label for="portfolio">Portfolio URL</label><input id="portfolio" name="portfolio" required></div>' if variant == "extra" else ""
    captcha = ""
    guard = ""
    if variant == "check":
        captcha = '<div data-captcha-challenge="text" hidden><p>Type the characters you see</p></div>'
        guard = """
          form.addEventListener('submit', (e) => {
            if (!form.dataset.verified) { e.preventDefault(); document.querySelector('[data-captcha-challenge]').hidden = false; }
          });"""
    return f"""<html><head><title>Backend Developer at Acme</title></head><body>
<form id="application-form" method="post" action="/ab/{org}/{posting_id}/apply?variant={variant}" enctype="multipart/form-data">
  <div><label for="_systemfield_name">Name</label><input id="_systemfield_name" name="_systemfield_name" required></div>
  <div><label for="_systemfield_email">Email</label><input id="_systemfield_email" name="_systemfield_email" required></div>
  <div><label for="_systemfield_phone">Phone</label><input id="_systemfield_phone" name="_systemfield_phone" required></div>
  <div><label for="_systemfield_resume">Resume</label><input id="_systemfield_resume" name="_systemfield_resume" type="file" required></div>
  <div><label for="location">Location</label><input id="location" name="location"></div>
  <div><label for="q_why">Why do you want to work at Acme?</label><textarea id="q_why" name="q_why" required></textarea></div>
  <div><label for="q_years">Years of experience</label><input id="q_years" name="q_years" role="combobox" aria-autocomplete="list" data-options="0-1 years|2-4 years|5+ years" required></div>
  <div><label for="gender">Gender</label><input id="gender" name="gender" role="combobox" aria-autocomplete="list" data-options="Woman|Man|I don't wish to answer" required></div>
  <fieldset><legend>How did you hear about this job?</legend>
    <label><input type="checkbox" name="q_hear" value="Job board"> Job board</label>
    <label><input type="checkbox" name="q_hear" value="Other"> Other</label>
  </fieldset>
  {extra}{captcha}
  <button type="submit">Submit application</button>
</form>
<script>
  const form = document.getElementById('application-form');
  document.querySelectorAll('[role=combobox]').forEach((input) => {{
    const opts = (input.dataset.options || '').split('|');
    const list = document.createElement('div');
    input.after(list);
    const show = () => {{
      const q = input.value.toLowerCase();
      list.innerHTML = opts.filter((o) => o.toLowerCase().includes(q)).map((o) => '<div role="option">' + o + '</div>').join('');
      list.querySelectorAll('[role=option]').forEach((el) => el.addEventListener('click', () => {{ input.value = el.textContent; list.innerHTML = ''; }}));
    }};
    input.addEventListener('input', show);
    input.addEventListener('focus', show);
  }});
  {guard}
</script></body></html>"""


@router.post("/ab-api/non-user-graphql")
async def ab_graphql(request: Request):
    body = await request.json()
    AB_OPERATIONS.append(str(body.get("operationName") or ""))
    query = str(body.get("query") or "")
    if query.strip().lower().startswith("mutation"):
        return JSONResponse({"errors": [{"message": "this fixture only answers the job posting read"}]}, status_code=400)
    posting_id = str((body.get("variables") or {}).get("jobPostingId") or POSTING_ID)
    return {"data": {"jobPosting": _posting(posting_id)}}


@router.get("/ab/{org}/{posting_id}", response_class=HTMLResponse)
def ab_job(org: str, posting_id: str, variant: str = "new"):
    return HTMLResponse(_form(org, posting_id, variant))


@router.post("/ab/{org}/{posting_id}/apply")
async def ab_apply(org: str, posting_id: str, request: Request, variant: str = "new"):
    form = await request.form()
    data: dict = {}
    for key in form.keys():
        vals = form.getlist(key)
        if hasattr(vals[0], "filename"):
            data[key] = {"filename": vals[0].filename, "bytes": len(await vals[0].read())}
        else:
            data[key] = vals if len(vals) > 1 else vals[0]
    if variant == "invalid":
        return HTMLResponse('<div class="field-error" role="alert">Phone must include an area code</div>', status_code=422)
    if not data.get("_systemfield_name") or not isinstance(data.get("_systemfield_resume"), dict):
        return HTMLResponse('<div class="field-error" role="alert">This field is required</div>', status_code=422)
    AB_SUBMISSIONS.append({"org": org, "posting_id": posting_id, "variant": variant, **data})
    if variant == "slow":
        time.sleep(0.2)
        return HTMLResponse(_form(org, posting_id, "slow"))
    return RedirectResponse(f"/ab/{org}/{posting_id}/thanks", status_code=303)


@router.get("/ab/{org}/{posting_id}/thanks", response_class=HTMLResponse)
def ab_thanks(org: str, posting_id: str):
    return HTMLResponse(
        f"<html><head><title>Application submitted</title></head><body><h1>Application submitted</h1><p>Thanks for applying to {org}. Reference {posting_id}.</p></body></html>"
    )
