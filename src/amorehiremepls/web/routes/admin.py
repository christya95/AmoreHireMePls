"""People an admin can add, extend, turn off or delete. Members are not registered here."""

from __future__ import annotations

from amorehiremepls.core.accounts import AccountError, access_summary, create_member, delete_user, extend_access, set_password, set_user_status, usage_totals
from amorehiremepls.web.chrome import Depends, Form, HTMLResponse, RedirectResponse, Request


def register(app, rt, require_admin, require_csrf, render) -> None:
    def _people() -> list[dict]:
        totals = usage_totals(rt.store)
        rows = []
        for user in rt.store.db["users"].find({"status": {"$ne": "deleted"}}).sort("created_at", 1):
            summary = access_summary(user)
            usage = totals.get(user["_id"]) or {"calls": 0, "cost": 0, "unknown": 0}
            rows.append(
                {
                    "id": user["_id"],
                    "login": user.get("login") or "",
                    "role": user.get("role") or "member",
                    "status": user.get("status") or "off",
                    "expired": summary["expired"],
                    "days_left": summary["days_left"] if summary["days_left"] is not None else 0,
                    "calls": usage["calls"],
                    "cost": usage["cost"],
                    "unknown": usage["unknown"],
                }
            )
        return rows

    @app.get("/admin", response_class=HTMLResponse)
    def people(request: Request, session: dict = Depends(require_admin)):
        rows = _people()
        return render(request, "people.html", session, people=rows, migrated=bool(rows))

    @app.post("/admin/people")
    def add_person(request: Request, session: dict = Depends(require_admin), csrf: str = Form(...), login: str = Form(""), password: str = Form(""), display_name: str = Form("")):
        require_csrf(request, session, csrf)
        try:
            create_member(rt.store, login, password, display_name)
        except AccountError:
            return RedirectResponse("/admin?err=person_refused", status_code=303)
        return RedirectResponse("/admin?ok=person_added", status_code=303)

    @app.post("/admin/people/password")
    def change_password(request: Request, session: dict = Depends(require_admin), csrf: str = Form(...), user_id: str = Form(...), password: str = Form(...)):
        require_csrf(request, session, csrf)
        try:
            set_password(rt.store, user_id, password)
        except AccountError:
            return RedirectResponse("/admin?err=person_refused", status_code=303)
        return RedirectResponse("/admin?ok=password_set", status_code=303)

    @app.get("/admin/people/{user_id}", response_class=HTMLResponse)
    def person(request: Request, user_id: str, session: dict = Depends(require_admin)):
        user = rt.store.db["users"].find_one({"_id": user_id, "status": {"$ne": "deleted"}})
        if not user:
            return RedirectResponse("/admin", status_code=303)
        summary = access_summary(user)
        rows = list(rt.store.db["usage"].find({"user_id": user_id}).sort("created_at", -1).limit(200))
        return render(
            request,
            "person.html",
            session,
            user_id=user_id,
            login=user.get("login") or "",
            role=user.get("role") or "member",
            status=user.get("status") or "off",
            expired=summary["expired"],
            days_left=summary["days_left"] or 0,
            rows=rows,
        )

    @app.post("/admin/people/{user_id}/extend")
    def extend(request: Request, user_id: str, session: dict = Depends(require_admin), csrf: str = Form(...), days: int = Form(365)):
        require_csrf(request, session, csrf)
        try:
            extend_access(rt.store, user_id, int(days))
        except (AccountError, ValueError):
            return RedirectResponse(f"/admin/people/{user_id}?err=person_refused", status_code=303)
        return RedirectResponse(f"/admin/people/{user_id}?ok=access_extended", status_code=303)

    @app.post("/admin/people/{user_id}/status")
    def status(request: Request, user_id: str, session: dict = Depends(require_admin), csrf: str = Form(...), status: str = Form(...)):
        require_csrf(request, session, csrf)
        try:
            set_user_status(rt.store, user_id, status)
        except AccountError:
            return RedirectResponse(f"/admin/people/{user_id}?err=person_refused", status_code=303)
        flag = "person_off" if status == "off" else "person_on"
        return RedirectResponse(f"/admin?ok={flag}", status_code=303)

    @app.post("/admin/people/{user_id}/delete")
    def remove(request: Request, user_id: str, session: dict = Depends(require_admin), csrf: str = Form(...), confirm: str = Form("")):
        require_csrf(request, session, csrf)
        if confirm.strip().lower() != "delete":
            return RedirectResponse(f"/admin/people/{user_id}?err=person_refused", status_code=303)
        try:
            delete_user(rt.store, user_id, rt.settings.private_dir / "user_backups")
        except AccountError:
            return RedirectResponse(f"/admin/people/{user_id}?err=person_refused", status_code=303)
        return RedirectResponse("/admin?ok=person_deleted", status_code=303)
