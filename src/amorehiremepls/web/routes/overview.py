"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    ATTENTION_STATUSES,
    HOSTS_ENV_EXAMPLE,
    Any,
    Depends,
    HTMLResponse,
    Request,
    donut,
    line_chart,
    month_buckets,
    next_action_label,
    next_slot_description,
    os,
    status_label,
    timedelta,
    to_local,
    toml_status,
    utcnow,
)


def register(app, rt, ui_store, require_session, render) -> None:
    # -- overview --------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, session: dict = Depends(require_session)):
        rt.reload_profile()
        s = rt.store
        counts = {
            "jobs": {r["_id"]: r["n"] for r in s.c("jobs").aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])},
            "applications": {r["_id"]: r["n"] for r in s.c("applications").aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])},
            "sync": rt.sync.outbox.summary(),
            "needs_verification": s.c("actions").count_documents({"status": "needs_verification"}),
        }
        runs = list(s.c("runs").find().sort("created_at", -1).limit(8))
        month_start = to_local(utcnow()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        usage_pipeline = [
            {
                "$group": {
                    "_id": None,
                    "calls": {"$sum": 1},
                    "in": {"$sum": "$input_tokens"},
                    "out": {"$sum": "$output_tokens"},
                    "cost": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
                    "unknown": {"$sum": {"$cond": [{"$eq": ["$cost_known", False]}, 1, 0]}},
                }
            }
        ]
        usage = next(iter(s.c("usage").aggregate(usage_pipeline)), {})
        usage_month = next(iter(s.c("usage").aggregate([{"$match": {"created_at": {"$gte": month_start}}}, *usage_pipeline])), {})
        kpis = overview_kpis(counts, usage_month)
        if session.get("role") == "member":
            kpis = [k for k in kpis if k["href"] != "/usage"]
            usage = {}
        return render(
            request,
            "index.html",
            session,
            counts=counts,
            runs=runs,
            usage=usage,
            usage_month=usage_month,
            kpis=kpis,
            chart=applications_chart(),
            pipeline=jobs_donut(counts["jobs"]),
            attention=attention_items(),
            activity=list(s.c("audit").find({"event": {"$nin": ["login", "login_failed"]}}).sort("created_at", -1).limit(10)),
            recent_jobs=list(s.c("jobs").find().sort("updated_at", -1).limit(6)),
            schedule=next_slot_description(rt),
            provider=rt.settings.effective_model or "not configured (default provider: openai)",
            provider_last=s.get_control("provider_last_check") or {},
            missing=rt.profile.missing_essentials(),
            checklist=None if ui_store.load().checklist_dismissed else getting_started(),
        )

    def getting_started() -> dict:
        """Setup checklist computed from real state. Each item links to the one place where it can be completed."""
        s, p, st = rt.store, rt.profile, rt.settings
        last = s.get_control("provider_last_check") or {}
        key_env = "OPENAI_API_KEY" if st.model_provider == "openai" else "ANTHROPIC_API_KEY"
        key_present = bool(os.environ.get(key_env))
        model = st.effective_model
        priced = bool(model) and rt.pricing.cost(model, 1000, 1000) is not None
        missing = p.missing_essentials()
        local_apps = {"provenance.origin": "local"}
        first_review = s.c("applications").find_one({**local_apps, "status": {"$in": ["ready_for_review", "needs_input"]}}, {"_id": 1})
        if not (model and key_present):
            provider_hint = "Set AMORE_MODEL and the API key in your environment, then run the check."
        elif last and not last.get("ok"):
            provider_hint = "Last check failed: " + str(last.get("error") or "")
        else:
            provider_hint = "Not checked yet."

        def item(key: str, label: str, done: bool, href: str, action: str, hint: str = "", code: str = "", method: str = "get") -> dict:
            return {"key": key, "label": label, "done": bool(done), "href": href, "action": action, "hint": hint, "code": code, "method": method}

        toml = toml_status(st.candidate_dir, s)
        imported_any = st.tracker_enabled and s.c("applications").count_documents({"provenance.origin": "tracker_import"}) > 0
        profile_hint = ("Missing: " + ", ".join(missing)) if missing else ""
        if toml.state in ("new", "changed"):
            profile_hint = "candidate/profile.toml is ready to load. " + profile_hint
        elif toml.state == "missing" and not p.facts:
            profile_hint = "Copy candidate/profile.example.toml to candidate/profile.toml, fill it in, then load it from the Profile page."
        items = [
            item("profile", "Confirm your profile facts", bool(p.facts) and not missing, "/profile", "Open profile", profile_hint),
            item(
                "base_resume",
                "Choose your base resume",
                p.base_resume is not None,
                "/profile",
                "Choose the DOCX",
                "Drop your approved base resume (DOCX) in the candidate folder, or paste where it is on this computer.",
            ),
            *([item("history", "Load your past applications", imported_any, "/history", "Load past applications")] if st.tracker_enabled else []),
            item(
                "provider",
                "Connect the AI model",
                bool(model) and key_present and bool(last.get("ok")),
                "/provider/check",
                "Check the connection",
                provider_hint,
                "" if model else "AMORE_MODEL=gpt-5.4-mini",
                "post",
            ),
            item(
                "hosts",
                "Allow the sites the browser may visit",
                bool(st.allowed_hosts),
                "/help#hosts",
                "How to set it",
                "Done in the environment only: add this line to .env (sites separated by commas) and restart both app processes.",
                HOSTS_ENV_EXAMPLE,
            ),
            item(
                "pricing",
                "Add prices for your AI model",
                priced,
                "/usage",
                "Open AI usage",
                "Add the model to config/pricing.json so costs show as numbers instead of 'unknown'." if model else "Set a model first.",
            ),
            item("job", "Add your first job", s.c("jobs").estimated_document_count() > 0, "/jobs", "Add a job"),
            item(
                "review",
                "Open your first application package",
                s.c("applications").count_documents({**local_apps, "review_package": {"$ne": None}}) > 0,
                "/applications",
                "Open applications",
                "Start from a job page; the app prepares the resume and answers in the background.",
            ),
            item(
                "authorization",
                "Grant your first approval",
                s.c("authorizations").estimated_document_count() > 0,
                f"/applications/{first_review['_id']}" if first_review else "/applications",
                "Read and approve" if first_review else "Open applications",
                "Each approval covers one exact package. Nothing is sent before you approve.",
            ),
        ]
        done = sum(1 for i in items if i["done"])
        return {"items": items, "done": done, "total": len(items), "complete": done == len(items)}

    def overview_kpis(counts: dict, usage_month: dict) -> list[dict]:
        jobs, apps, sync = counts["jobs"], counts["applications"], counts["sync"]
        prepared = sum(apps.get(k, 0) for k in ("ready_for_review", "needs_input", "authorized", "handoff"))
        submitted = apps.get("submitted", 0)
        pending = sum(sync.get(k, 0) for k in ("sync_pending", "local_only", "committed_local"))
        needs_ver = counts["needs_verification"] + apps.get("needs_verification", 0)
        calls = usage_month.get("calls", 0) or 0
        unknown = usage_month.get("unknown", 0) or 0
        if not calls:
            spend, spend_note = "$0.00", "no AI calls this month"
        elif unknown:
            spend, spend_note = f"${usage_month.get('cost', 0):.2f}+", f"{unknown} of {calls} AI calls without a known price"
        else:
            spend, spend_note = f"${usage_month.get('cost', 0):.2f}", f"{calls} AI calls this month"
        conflicts = sync.get("conflict", 0)

        def kpi(label: str, value: Any, note: str, icon: str, tone: str, href: str, pill: str, pill_tone: str) -> dict:
            return {"label": label, "value": value, "note": note, "icon": icon, "tone": tone, "href": href, "pill": pill, "pill_tone": pill_tone}

        return [
            kpi(
                "Jobs to look at",
                jobs.get("new", 0) + jobs.get("needs_review", 0),
                f"{jobs.get('new', 0)} new · {jobs.get('needs_review', 0)} need your decision",
                "jobs",
                "",
                "/jobs",
                f"{jobs.get('qualified', 0)} good fit",
                "ok",
            ),
            kpi(
                "Applications sent",
                submitted,
                f"{prepared} prepared and waiting for you",
                "applications",
                "ok",
                "/applications",
                f"{prepared} prepared",
                "warn" if prepared else "neutral",
            ),
            kpi(
                "Results unclear",
                needs_ver,
                "check these on the job site",
                "alert",
                "bad" if needs_ver else "",
                "/applications?status_filter=needs_verification",
                "needs you" if needs_ver else "all clear",
                "bad" if needs_ver else "ok",
            ),
            kpi(
                "Waiting to save to GitHub",
                pending,
                f"{conflicts} need you · {sync.get('synced', 0)} saved",
                "sync",
                "warn" if pending else "",
                "/sync",
                "needs you" if conflicts else ("waiting" if pending else "up to date"),
                "bad" if conflicts else ("warn" if pending else "ok"),
            ),
            kpi("AI cost this month", spend, spend_note, "dollar", "info", "/usage", "some prices unknown" if unknown else "measured", "warn" if unknown else "neutral"),
        ]

    def applications_chart() -> dict:
        buckets = month_buckets(12)
        keys = {k: 0 for k, _ in buckets}
        for a in rt.store.c("applications").find({"status": "submitted"}, {"application_date": 1, "record.application_date": 1, "updated_at": 1}):
            day = a.get("application_date") or (a.get("record") or {}).get("application_date")
            key = str(day)[:7] if day else to_local(a.get("updated_at")).strftime("%Y-%m") if a.get("updated_at") else None
            if key in keys:
                keys[key] += 1
        return line_chart([(label, keys[k]) for k, label in buckets])

    def jobs_donut(by_status: dict) -> dict:
        order = [("new", "c1"), ("needs_review", "c4"), ("qualified", "c8"), ("applied", "c2"), ("applied_historical", "c3"), ("rejected", "c5")]
        items = [(k, by_status.get(k, 0), cls) for k, cls in order if by_status.get(k)]
        other = sum(v for k, v in by_status.items() if k not in dict(order))
        if other:
            items.append(("other", other, "c6"))
        return donut(items)

    def attention_items() -> list[dict]:
        s = rt.store
        items: list[dict] = []
        titles = {
            "ready_for_review": ("Waiting for your approval", "Read the package and approve this exact application.", "warn", "key"),
            "needs_input": ("Needs your answers", "The form asks questions your profile cannot answer.", "warn", "help"),
            "handoff": ("Finish on the site yourself", "Apply on the job site, then record the confirmation here.", "info", "hand"),
            "needs_verification": ("Result unclear", "Check the job site and record what you saw.", "bad", "alert"),
        }

        def item(title: str, detail: str, hint: str, tone: str, icon: str, href: str, action: str) -> dict:
            return {"title": title, "detail": detail, "hint": hint, "tone": tone, "icon": icon, "href": href, "action": action}

        fresh = utcnow() - timedelta(seconds=20)
        holds = list(s.c("human_holds").find({"status": "holding", "heartbeat_at": {"$gte": fresh}}).sort("started_at", -1).limit(8))
        hold_jobs = {j["_id"]: j for j in s.c("jobs").find({"_id": {"$in": [h["job_id"] for h in holds]}}, {"employer": 1, "title": 1})}
        for hold in holds:
            j = hold_jobs.get(hold["job_id"], {})
            items.append(
                item(
                    "Human check needed",
                    f"{j.get('employer', '?')} · {j.get('title', '')}",
                    "Finish the check if you can see the page, then press Continue. If you cannot see it, wait.",
                    "warn",
                    "alert",
                    f"/applications/{hold['application_id']}",
                    "Continue",
                )
            )

        apps = list(s.c("applications").find({"status": {"$in": list(ATTENTION_STATUSES)}, "provenance.origin": "local"}).sort("updated_at", -1).limit(12))
        jobs = {j["_id"]: j for j in s.c("jobs").find({"_id": {"$in": [a["job_id"] for a in apps]}}, {"employer": 1, "title": 1})}
        for a in apps:
            t, d, tone, icon = titles[a["status"]]
            j = jobs.get(a["job_id"], {})
            items.append(item(t, f"{j.get('employer', '?')} · {j.get('title', '')}", d, tone, icon, f"/applications/{a['_id']}", next_action_label(a["status"])))
        for r in s.c("runs").find({"status": {"$in": ["waiting", "paused", "failed"]}}).sort("updated_at", -1).limit(8):
            it = r.get("interrupt") or {}
            if it.get("application_id") and any(x["href"].endswith(it["application_id"]) for x in items):
                continue
            label = {"waiting": f"Task waiting: {status_label(it.get('type') or 'interrupt').lower()}", "paused": "Task paused", "failed": "Task failed"}[r["status"]]
            tone = "bad" if r["status"] == "failed" else "warn"
            verb = {"waiting": "Open task", "paused": "Resume task", "failed": "See what failed"}[r["status"]]
            items.append(item(label, f"{status_label(r['kind'])} {r.get('params', {}).get('job_id', '')}", r.get("error") or "", tone, "runs", f"/runs/{r['_id']}", verb))
        review = s.c("jobs").count_documents({"status": "needs_review"})
        if review:
            detail = f"{review} job{'s' if review != 1 else ''} the rules could not decide on"
            items.append(item("Jobs need your decision", detail, "Decide whether each one is worth preparing.", "info", "jobs", "/jobs?status_filter=needs_review", "Look at jobs"))
        held = s.c("recipients").count_documents({"history_reconciled_at": {"$exists": False}})
        if held:
            detail = f"{held} {'person' if held == 1 else 'people'} held before a first message"
            hint = "Confirm you have not messaged them before."
            items.append(item("People waiting for a contact check", detail, hint, "info", "outreach", "/outreach", "Check contacts"))
        return items
