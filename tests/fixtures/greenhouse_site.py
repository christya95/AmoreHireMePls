"""Greenhouse style fixture board (fictional employer "Acme", job 123456) served by the fixture ATS site.

Routes
  GET  /gh-api/v1/boards/{org}/jobs/{id}?questions=true   Job Board API shaped JSON (questions, location, demographic)
  GET  /{org}/jobs/{id}?variant=new|legacy|invalid|check|grid|slow|chooser|noresume
        new      React board layout: form#application-form, ids = field names, combobox single selects with
                 [role=option] menus, checkbox groups `question_N[]_<value>`, #country and #candidate-location
                 suggestion boxes, demographic combobox under a numeric id, JS submit + redirect. File uploads mirror
                 the real board: a "Resume/CV" group (`role=group aria-labelledby=upload-label-resume`) with Attach /
                 Dropbox / Google Drive / Enter manually buttons and a visually hidden `input#resume[type=file]`
                 **without a name**; choosing a file removes the input, shows a progress bar, then the filename
        legacy   form#application_form, native <select>s, radios, `job_application_answers_attributes_N_*` ids,
                 labels only (no field-name ids for custom questions), #submit_app, plain form POST
        chooser  like `new` but there is no file input until Attach is clicked (the button creates one and opens the picker)
        noresume like `new` but the Resume/CV group offers only "Enter manually": no file input, no Attach button
        stuck    like `new` but the upload progress bar never gives way to the filename row
        missing  like `new` but the Phone control is not on the page (the form check must notice before any typing)
        invalid  like `new` but the server rejects the submit with a field error (nothing recorded)
        check    like `new` but the submit first shows a text captcha widget (SVG picture, known answer)
        grid     like `new` but the submit first shows a 3x3 image grid challenge
        slow     like `new` but the server records the submission and never redirects (unclear result)
  GET  /embed/{org}/{id}                                company page embedding the legacy form in #grnhse_iframe
  POST /{org}/jobs/{id}/submit                          records the submission; validation / slow behaviour by variant
  GET  /{org}/jobs/{id}/confirmation                    "Thank you for applying. Your application has been received."
  POST /captcha/verify                                  checks a challenge answer (text or grid cells)
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

router = APIRouter()
GH_SUBMISSIONS: list[dict] = []
CAPTCHA_ANSWERS: dict[str, dict] = {}  # token -> {"kind", "answer", "attempts"}
TEXT_CAPTCHA_ANSWER = "7K3P"
GRID_CAPTCHA_CELLS = [0, 4, 8]

ORG, JOB_ID = "acme", "123456"


def board_schema(org: str, job_id: str) -> dict:
    sel = lambda name, *labels: [{"name": name, "type": "multi_value_single_select", "values": [{"value": 10 + i, "label": lab} for i, lab in enumerate(labels)]}]  # noqa: E731
    return {
        "id": int(job_id),
        "internal_job_id": 99,
        "title": "Backend Developer",
        "company_name": org.title(),
        "requisition_id": "R-" + job_id,
        "absolute_url": f"/{org}/jobs/{job_id}",
        "location": {"name": "Toronto, ON"},
        "content": "<p>Fictional posting.</p>",
        "questions": [
            {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
            {"label": "Last Name", "required": True, "fields": [{"name": "last_name", "type": "input_text", "values": []}]},
            {"label": "Preferred First Name", "required": False, "fields": [{"name": "preferred_name", "type": "input_text", "values": []}]},
            {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text", "values": []}]},
            {"label": "Phone", "required": True, "fields": [{"name": "phone", "type": "input_text", "values": []}]},
            {
                "label": "Resume/CV",
                "required": True,
                "fields": [{"name": "resume", "type": "input_file", "values": []}, {"name": "resume_text", "type": "textarea", "values": []}],
            },
            {
                "label": "Cover Letter",
                "required": False,
                "fields": [{"name": "cover_letter", "type": "input_file", "values": []}, {"name": "cover_letter_text", "type": "textarea", "values": []}],
            },
            {"label": "LinkedIn Profile", "required": False, "fields": [{"name": "question_1001", "type": "input_text", "values": []}]},
            {"label": "Website", "required": False, "fields": [{"name": "question_1002", "type": "input_text", "values": []}]},
            {"label": "Please select your current province of residence.", "required": True, "fields": sel("question_1003", "Alberta", "Ontario", "Quebec", "Not in Canada")},
            {"label": "This role is hybrid. Can you commit to three days per week in the office?", "required": True, "fields": sel("question_1004", "Yes", "No")},
            {
                "label": "How many years of full-time experience do you have, excluding internships/co-ops?",
                "required": True,
                "fields": sel("question_1005", "Less than 2 years", "2-4 years", "4+ years"),
            },
            {"label": "Why do you want to work at Acme?", "required": True, "fields": [{"name": "question_1006", "type": "textarea", "values": []}]},
            {
                "label": "How did you hear about Acme? (Select all that apply)",
                "required": True,
                "fields": [
                    {
                        "name": "question_1007[]",
                        "type": "multi_value_multi_select",
                        "values": [{"value": 501, "label": "Job board"}, {"value": 502, "label": "A friend"}, {"value": 503, "label": "Other"}],
                    }
                ],
            },
            {"label": "Are you legally authorized to work in Canada?", "required": True, "fields": sel("question_1008", "Yes", "No")},
        ],
        "location_questions": [
            {"label": "Longitude", "required": True, "fields": [{"name": "longitude", "type": "input_hidden", "values": []}]},
            {"label": "Latitude", "required": True, "fields": [{"name": "latitude", "type": "input_hidden", "values": []}]},
            {"label": "Location", "required": True, "fields": [{"name": "location", "type": "input_text", "values": []}]},
        ],
        "compliance": None,
        "demographic_questions": {
            "header": "Demographic questions",
            "description": "<p>Voluntary.</p>",
            "questions": [
                {
                    "id": 4000,
                    "label": "How do you currently describe your gender identity?",
                    "required": True,
                    "type": "multi_value_single_select",
                    "answer_options": [
                        {"id": 41, "label": "Man, male or masculine", "free_form": False},
                        {"id": 42, "label": "Woman, female or feminine", "free_form": False},
                        {"id": 43, "label": "I don't wish to answer", "free_form": False},
                    ],
                },
                {
                    "id": 4001,
                    "label": "Which categories describe you? Select all that apply",
                    "required": False,
                    "type": "multi_value_multi_select",
                    "answer_options": [{"id": 51, "label": "Category A", "free_form": False}, {"id": 52, "label": "I don't wish to answer", "free_form": False}],
                },
            ],
        },
        "data_compliance": [{"type": "gdpr", "requires_consent": False}],
    }


@router.get("/gh-api/v1/boards/{org}/jobs/{job_id}")
def api_job(org: str, job_id: str, questions: str = "false"):
    if org != ORG or job_id != JOB_ID:
        return JSONResponse({"error": "not found"}, status_code=404)
    data = board_schema(org, job_id)
    if questions != "true":
        for k in ("questions", "location_questions", "compliance", "demographic_questions"):
            data.pop(k, None)
    return JSONResponse(data)


CSS = """
<style>
.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.menu{border:1px solid #999;background:#fff;max-width:320px}.menu[hidden]{display:none}
[role=option]{padding:2px 6px;cursor:pointer}[role=option]:hover{background:#eee}
.helper-text--error{color:#b00020}
[data-captcha-challenge]{border:2px solid #c60;padding:8px;margin:8px 0;max-width:360px}
[data-captcha-challenge][hidden]{display:none}
[data-cell]{display:inline-block;width:60px;height:60px;margin:2px;background:#ddd;border:2px solid transparent;text-align:center;line-height:60px}
[data-cell].picked{border-color:#06c}
</style>"""

JS = """
<script>
function combobox(input, options, multi) {
  const menu = document.createElement('div'); menu.className = 'menu'; menu.setAttribute('role', 'listbox'); menu.hidden = true;
  input.insertAdjacentElement('afterend', menu);
  const hidden = document.querySelector(`input[type=hidden][data-for="${input.id}"]`);
  const render = () => {
    const q = input.value.toLowerCase(); menu.innerHTML = '';
    options.filter(o => !q || o.toLowerCase().includes(q)).forEach(o => {
      const d = document.createElement('div'); d.setAttribute('role', 'option'); d.textContent = o;
      d.addEventListener('mousedown', ev => { ev.preventDefault(); input.value = multi ? '' : o;
        if (hidden) hidden.value = multi ? (hidden.value ? hidden.value + '|' + o : o) : o; menu.hidden = true; });
      menu.appendChild(d); });
    menu.hidden = menu.children.length === 0;
  };
  input.addEventListener('focus', render); input.addEventListener('input', render);
  input.addEventListener('blur', () => setTimeout(() => menu.hidden = true, 150));
}
document.querySelectorAll('input[role=combobox]').forEach(el => combobox(el, JSON.parse(el.dataset.options), el.dataset.multi === '1'));
// File uploads as on the real React board: the chosen file is kept in memory, the input leaves the DOM, a progress
// bar shows, then the filename replaces the buttons. The form has no named file input; the submit appends the file.
const chosenFiles = {};
function fileGroup(group) {
  const name = group.dataset.upload;
  const wrap = group.querySelector('.file-upload__wrapper');
  const onFile = (file) => {
    chosenFiles[name] = file;
    wrap.innerHTML = '<div role="progressbar" aria-valuenow="0" class="file-upload__progressbar"><span style="width:0%"></span></div>';
    if (document.getElementById('application-form').dataset.variant === 'stuck') return;  // upload never finishes: no filename row
    setTimeout(() => { wrap.innerHTML = ''; const row = document.createElement('div'); row.className = 'file-upload__filename'; row.textContent = file.name;
      const rm = document.createElement('button'); rm.type = 'button'; rm.setAttribute('aria-label', 'Remove file'); rm.textContent = 'x'; wrap.append(row, rm); }, 400);
  };
  const input = group.querySelector('input[type=file]');
  if (input) input.addEventListener('change', () => input.files[0] && onFile(input.files[0]));
  const attach = group.querySelector('button[data-attach]');
  if (attach && !input) attach.addEventListener('click', () => {
    const i = document.createElement('input'); i.type = 'file'; i.className = 'visually-hidden'; i.addEventListener('change', () => i.files[0] && onFile(i.files[0]));
    attach.parentElement.appendChild(i); i.click(); });
}
document.querySelectorAll('[data-upload]').forEach(fileGroup);
const form = document.getElementById('application-form');
if (form) form.addEventListener('submit', async ev => {
  ev.preventDefault();
  document.querySelectorAll('.helper-text--error').forEach(e => e.remove());
  const variant = form.dataset.variant;
  if ((variant === 'check' || variant === 'grid') && !form.dataset.verified) {
    const widget = document.querySelector('[data-captcha-challenge]'); widget.hidden = false; return;
  }
  const body = new FormData(form);
  for (const [k, f] of Object.entries(chosenFiles)) body.append(k, f, f.name);
  const r = await fetch(form.dataset.submit, {method: 'POST', body});
  const data = await r.json();
  if (data.ok) { if (data.redirect) location.href = data.redirect; return; }
  (data.errors || []).forEach(e => { const el = document.getElementById(e.field) || form;
    const d = document.createElement('div'); d.className = 'helper-text--error'; d.setAttribute('role', 'alert'); d.textContent = e.message;
    el.insertAdjacentElement('afterend', d); });
});
const widget = document.querySelector('[data-captcha-challenge]');
if (widget) {
  const picked = new Set();
  widget.querySelectorAll('[data-cell]').forEach(c => c.addEventListener('click', () => { const i = +c.dataset.cell;
    if (picked.has(i)) { picked.delete(i); c.classList.remove('picked'); } else { picked.add(i); c.classList.add('picked'); } }));
  widget.querySelector('[data-captcha-submit]').addEventListener('click', async () => {
    const fd = new FormData(); fd.append('token', widget.dataset.token);
    const box = widget.querySelector('input[type=text]'); fd.append('answer', box ? box.value : Array.from(picked).sort().join(','));
    const r = await fetch('/captcha/verify', {method: 'POST', body: fd}); const data = await r.json();
    if (data.ok) { widget.hidden = true; form.dataset.verified = '1'; form.requestSubmit(); }
    else { widget.querySelector('[data-captcha-status]').textContent = 'That was not right, try again (' + data.attempts + ')';
           if (box) box.value = ''; picked.clear(); widget.querySelectorAll('[data-cell]').forEach(c => c.classList.remove('picked')); }
  });
}
</script>"""


def _options_json(values: list[str]) -> str:
    return json.dumps(values).replace('"', "&quot;")


def _file_group(name: str, label: str, variant: str) -> str:
    """Resume/CV or Cover Letter group as the real board renders it (read-only inspection of a live posting, 2026-11-05)."""
    heading = f'<div id="upload-label-{name}" class="label upload-label">{label.rstrip("*")}{"<span class=required>*</span>" if label.endswith("*") else ""}</div>'
    hidden_input = f'<input id="{name}" class="visually-hidden" type="file" accept=".pdf,.doc,.docx,.txt,.rtf">'
    if variant == "noresume" and name == "resume":
        attach = ""
    elif variant == "chooser" and name == "resume":
        attach = '<div class="secondary-button"><div><button type="button" class="btn btn--rectangle" data-attach>Attach</button></div></div>'
    else:
        attach = f'<div class="secondary-button"><div><button type="button" class="btn btn--rectangle" data-attach>Attach</button><label class="visually-hidden" for="{name}">Attach</label>{hidden_input}</div></div>'
    others = (
        f'<div class="secondary-button"><button type="button" class="btn btn--rectangle" data-testid="{name}-dropbox">Dropbox</button></div>'
        '<div class="secondary-button"><button type="button" class="btn btn--rectangle">Google Drive</button></div>'
        f'<div class="secondary-button"><div><button type="button" class="btn btn--rectangle" data-testid="{name}-text">Enter manually</button><label class="visually-hidden" for="{name}_text">Enter manually</label></div></div>'
    )
    return (
        f'<div role="group" aria-labelledby="upload-label-{name}" class="file-upload" data-upload="{name}">{heading}'
        f'<div class="file-upload__wrapper"><div class="button-container">{attach}{others}<p class="file-upload__filetypes">Accepted file types: pdf, doc, docx, txt, rtf</p></div></div></div>'
    )


def _new_layout(org: str, job_id: str, variant: str) -> str:
    s = board_schema(org, job_id)
    token = uuid.uuid4().hex[:10]
    CAPTCHA_ANSWERS[token] = {
        "kind": "grid" if variant == "grid" else "text",
        "answer": ",".join(map(str, GRID_CAPTCHA_CELLS)) if variant == "grid" else TEXT_CAPTCHA_ANSWER,
        "attempts": 0,
    }
    rows = []
    for q in s["questions"]:
        for f in q["fields"]:
            name, t = f["name"], f["type"]
            label = q["label"] + ("*" if q["required"] else "")
            if name.endswith("_text") or (variant == "missing" and name == "phone"):
                continue
            if t == "input_file":
                rows.append(_file_group(name, label, variant))
            elif t == "textarea":
                rows.append(f'<div class="input-wrapper"><label for="{name}">{label}</label><textarea id="{name}" name="{name}"></textarea></div>')
            elif t == "multi_value_single_select":
                opts = [v["label"] for v in f["values"]]
                rows.append(
                    f'<div class="select-shell"><label for="{name}">{label}</label><input type="text" id="{name}" role="combobox" aria-autocomplete="list" autocomplete="off" class="select__input" data-options="{_options_json(opts)}">'
                    f'<input type="hidden" name="{name}" data-for="{name}"></div>'
                )
            elif t == "multi_value_multi_select":
                base = name[:-2]
                boxes = "".join(f'<label><input type="checkbox" id="{base}[]_{v["value"]}" name="{name}" value="{v["value"]}"> {v["label"]}</label>' for v in f["values"])
                rows.append(f"<fieldset><legend>{label}</legend>{boxes}</fieldset>")
            else:
                typ = "tel" if name == "phone" else "text"
                rows.append(f'<div class="input-wrapper"><label for="{name}">{label}</label><input type="{typ}" id="{name}" name="{name}" class="input input__single-line"></div>')
    rows.append(
        '<div class="select-shell"><label for="country">Country*</label><input type="text" id="country" role="combobox" aria-autocomplete="list" class="select__input" '
        f'data-options="{_options_json(["Canada", "Cambodia", "United States"])}"><input type="hidden" name="country" data-for="country"></div>'
    )
    rows.append(
        '<div class="select-shell"><label for="candidate-location">Location (City)*</label><input type="text" id="candidate-location" role="combobox" aria-autocomplete="list" class="select__input" '
        f'data-options="{_options_json(["Milton Keynes, England, United Kingdom", "Milton, Ontario, Canada", "Toronto, Ontario, Canada"])}"><input type="hidden" name="location" data-for="candidate-location"></div>'
    )
    for q in s["demographic_questions"]["questions"]:
        qid = str(q["id"])
        opts = [o["label"] for o in q["answer_options"]]
        multi = q["type"] == "multi_value_multi_select"
        rows.append(
            f'<div class="select-shell"><label for="{qid}">{q["label"]}{"*" if q["required"] else ""}</label><input type="text" id="{qid}" role="combobox" aria-autocomplete="list" class="select__input" '
            f'data-options="{_options_json(opts)}" data-multi="{int(multi)}"><input type="hidden" name="demographic_{qid}" data-for="{qid}"></div>'
        )
    if variant == "grid":
        cells = "".join(f'<span data-cell="{i}">{"●" if i in GRID_CAPTCHA_CELLS else "▲"}</span>' for i in range(9))
        challenge = (
            f'<div data-captcha-challenge="grid" data-token="{token}" data-grid-cols="3" hidden><p data-captcha-instruction>Select all tiles with a circle</p>'
            f'<div data-captcha-grid style="width:200px">{cells}</div><p data-captcha-status></p><button type="button" data-captcha-submit>Verify</button></div>'
        )
    else:
        challenge = (
            f'<div data-captcha-challenge="text" data-token="{token}" hidden><p data-captcha-instruction>Type the characters you see</p>'
            f'<img data-captcha-image alt="" src="/captcha/{token}.svg" width="160" height="50"><br><input type="text" name="captcha_answer" aria-label="Characters">'
            f'<p data-captcha-status></p><button type="button" data-captcha-submit>Verify</button></div>'
        )
    return f"""<html><head><title>Job Application for Backend Developer at Acme</title>{CSS}</head><body>
<h1>Backend Developer</h1><p>Toronto, ON</p><div class="grecaptcha-badge" style="visibility:hidden">protected by reCAPTCHA</div>
<form id="application-form" class="application--form" data-variant="{variant}" data-submit="/{org}/jobs/{job_id}/submit?variant={variant}">
{"".join(rows)}
{challenge}
<button type="submit" class="btn btn--rectangle">Submit application</button>
</form>{JS}</body></html>"""


def _legacy_layout(org: str, job_id: str) -> str:
    """Older Greenhouse markup: no field-name ids for custom questions, native selects, radios, #submit_app."""
    s = board_schema(org, job_id)
    rows = []
    n = 0
    for q in s["questions"]:
        for f in q["fields"]:
            name, t = f["name"], f["type"]
            if name.endswith("_text"):
                continue
            std = name in ("first_name", "last_name", "preferred_name", "email", "phone", "resume", "cover_letter")
            fid = name if std else f"job_application_answers_attributes_{n}_{'boolean_value' if t == 'multi_value_single_select' and len(f['values']) == 2 else 'text_value'}"
            name = f"job_application[{name}]" if std else f"job_application[answers_attributes][{n}][text_value]"  # real legacy names: no field-name hooks
            if not std:
                n += 1
            label = q["label"]
            if t == "input_file":
                rows.append(f'<div data-field="{name}"><label for="{fid}">{label}</label><input type="file" id="{fid}" name="{name}"></div>')
            elif t == "textarea":
                rows.append(f'<div><label for="{fid}">{label}</label><textarea id="{fid}" name="{name}"></textarea></div>')
            elif t == "multi_value_single_select" and len(f["values"]) == 2:
                radios = "".join(f'<label><input type="radio" name="{name}" value="{v["value"]}"> {v["label"]}</label>' for v in f["values"])
                rows.append(f'<fieldset><legend id="lg_{n}">{label}</legend>{radios}</fieldset>')
            elif t == "multi_value_single_select":
                opts = '<option value="">Please select</option>' + "".join(f'<option value="{v["value"]}">{v["label"]}</option>' for v in f["values"])
                rows.append(f'<div><label for="{fid}">{label}</label><select id="{fid}" name="{name}">{opts}</select></div>')
            elif t == "multi_value_multi_select":
                boxes = "".join(f'<label><input type="checkbox" name="{name}" value="{v["value"]}"> {v["label"]}</label>' for v in f["values"])
                rows.append(f"<fieldset><legend>{label}</legend>{boxes}</fieldset>")
            else:
                rows.append(f'<div><label for="{fid}">{label}</label><input type="text" id="{fid}" name="{name}"></div>')
    rows.append(
        '<div><label for="job_application_location">Location</label><input type="text" id="job_application_location" name="location" role="combobox" aria-autocomplete="list" data-options="[&quot;Milton, Ontario, Canada&quot;]"></div>'
    )
    opts = '<option value="">Please select</option>' + "".join(
        f'<option value="{o["id"]}">{o["label"]}</option>' for o in s["demographic_questions"]["questions"][0]["answer_options"]
    )
    rows.append(
        f'<div><label for="job_application_demographic_answers_4000">How do you currently describe your gender identity?</label><select id="job_application_demographic_answers_4000" name="demographic_4000">{opts}</select></div>'
    )
    return f"""<html><head><title>Job Application for Backend Developer at Acme</title>{CSS}</head><body><div id="app_body">
<form id="application_form" method="post" action="/{org}/jobs/{job_id}/submit?variant=legacy" enctype="multipart/form-data">
{"".join(rows)}
<input type="submit" id="submit_app" value="Submit Application">
</form></div>{JS}</body></html>"""


@router.get("/{org}/jobs/{job_id}", response_class=HTMLResponse)
def gh_job(org: str, job_id: str, variant: str = "new"):
    if variant == "legacy":
        return HTMLResponse(_legacy_layout(org, job_id))
    return HTMLResponse(_new_layout(org, job_id, variant))


@router.get("/embed/{org}/{job_id}", response_class=HTMLResponse)
def gh_embed(org: str, job_id: str):
    return HTMLResponse(
        f'<html><head><title>Careers at Acme</title></head><body><h1>Join Acme</h1><iframe id="grnhse_iframe" src="/{org}/jobs/{job_id}?variant=legacy" width="900" height="1400"></iframe></body></html>'
    )


@router.get("/captcha/{token}.svg")
def gh_captcha_svg(token: str):
    text = CAPTCHA_ANSWERS.get(token, {}).get("answer", "????")
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="50"><rect width="160" height="50" fill="#f3efe4"/><text x="12" y="36" font-size="30" font-family="monospace" fill="#333" transform="skewX(-8)">{text}</text><line x1="0" y1="25" x2="160" y2="30" stroke="#999"/></svg>'
    return Response(svg, media_type="image/svg+xml")


@router.post("/captcha/verify")
def gh_captcha_verify(token: str = Form(...), answer: str = Form("")):
    entry = CAPTCHA_ANSWERS.get(token)
    if entry is None:
        return JSONResponse({"ok": False, "attempts": 0})
    entry["attempts"] += 1
    ok = answer.strip().replace(" ", "").upper() == entry["answer"].upper()
    return JSONResponse({"ok": ok, "attempts": entry["attempts"]})


@router.post("/{org}/jobs/{job_id}/submit")
async def gh_submit(request: Request, org: str, job_id: str, variant: str = "new"):
    form = await request.form()
    data: dict = {}
    for raw_key in form.keys():
        vals = form.getlist(raw_key)
        k = raw_key[16:-1] if raw_key.startswith("job_application[") and raw_key.count("[") == 1 else raw_key  # legacy job_application[x] -> x
        if hasattr(vals[0], "filename"):
            data[k] = {"filename": vals[0].filename, "bytes": len(await vals[0].read())}
        else:
            data[k] = vals if len(vals) > 1 else vals[0]
    errors = []
    if variant == "invalid":
        errors.append({"field": "phone", "message": "Phone must include an area code"})
    for req in ("first_name", "last_name", "email", "phone", "question_1006"):
        if not data.get(req) and not (variant == "legacy" and req == "question_1006"):
            errors.append({"field": req, "message": "This field is required"})
    if not isinstance(data.get("resume"), dict) or not data["resume"].get("filename"):
        errors.append({"field": "resume", "message": "Resume is required"})
    if errors:
        if variant == "legacy":
            return HTMLResponse("".join(f'<div class="field-error" role="alert">{e["message"]}</div>' for e in errors), status_code=422)
        return JSONResponse({"ok": False, "errors": errors})
    GH_SUBMISSIONS.append({"org": org, "job_id": job_id, "variant": variant, **data})
    if variant == "slow":
        time.sleep(4)
        return JSONResponse({"ok": True, "redirect": None})
    if variant == "legacy":
        return RedirectResponse(f"/{org}/jobs/{job_id}/confirmation", status_code=303)
    return JSONResponse({"ok": True, "redirect": f"/{org}/jobs/{job_id}/confirmation"})


@router.get("/{org}/jobs/{job_id}/confirmation", response_class=HTMLResponse)
def gh_confirmation(org: str, job_id: str):
    return HTMLResponse(
        "<html><head><title>Thank you for applying</title></head><body><h1>Thank you for applying.</h1>"
        "<p>Your application has been received. If there is a fit, someone will be getting back to you.</p><a href='/'>View more jobs at Acme</a></body></html>"
    )
