"""Owner notifications: one note per run and state, by email and an optional webhook.

The note names the job, says why it was sent, and links to the dashboard. It never includes answers,
resume text, or secrets. Delivery goes through `ActionStore` (`owner_notification`) so a replay does
not send twice; the `notifications` collection is the idempotency record for the run+state pair.
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from .actions import ActionFailed, ActionStore
from .config import Settings
from .db import Store
from .util import new_id, sha256_json, utcnow

log = logging.getLogger(__name__)

# Fixed sentences. Callers do not pass page text or form answers into the body.
REASONS: dict[str, str] = {
    "human_check": "The job site asked for a human check. Open the dashboard to finish it.",
    "needs_verification": "The app could not tell whether this application was sent. Check the job site and record what you saw.",
    "handoff": "This application needs you to finish it on the job site.",
    "submitted": "The employer site showed a confirmation. The application is recorded.",
    "test": "This is a test notification. If you can read it, notifications from the dashboard are working.",
}

SUBJECTS: dict[str, str] = {
    "human_check": "Human check needed",
    "needs_verification": "Result unclear",
    "handoff": "Needs you to finish",
    "submitted": "Application confirmed",
    "test": "Test notification",
}


class Notifier:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        actions: ActionStore,
        smtp_send: Callable[[EmailMessage], None] | None = None,
        http_post: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.actions = actions
        self._smtp_send = smtp_send
        self._http_post = http_post

    def channels(self) -> dict[str, bool]:
        return {"email": self.email_ready(), "webhook": self.webhook_ready()}

    def email_ready(self) -> bool:
        s = self.settings
        return bool(s.notify_email_to and s.smtp_host and s.smtp_login and s.smtp_password.get_secret_value())

    def webhook_ready(self) -> bool:
        return bool((self.settings.notify_webhook_url or "").strip())

    def notify(self, *, state: str, job: dict, application: dict, run_id: str | None) -> dict[str, Any]:
        """Send once for this run (or application, when no run exists) and state. No-op when nothing is configured."""
        if state not in REASONS or state == "test":
            raise ValueError(f"unknown notification state {state}")
        if not self.email_ready() and not self.webhook_ready():
            return {"sent": False, "deduped": False, "reason": "not_configured"}
        run_key = run_id or application["_id"]
        key = f"notify:{run_key}:{state}"
        return self._send_once(
            key,
            state,
            target_id=application["_id"],
            link=f"{self.settings.dashboard_base_url()}/applications/{application['_id']}",
            employer=str(job.get("employer") or ""),
            title=str(job.get("title") or ""),
            extra={"run_id": run_id, "application_id": application["_id"], "job_id": job.get("_id")},
        )

    def send_test(self) -> dict[str, Any]:
        """One test note. Each click gets its own key so a test can be repeated; the body stays generic."""
        if not self.email_ready() and not self.webhook_ready():
            return {"sent": False, "deduped": False, "reason": "not_configured"}
        key = f"notify:test:{new_id('test')}"
        return self._send_once(
            key,
            "test",
            target_id="settings",
            link=f"{self.settings.dashboard_base_url()}/settings",
            employer="",
            title="AmoreHireMePls",
            extra={"run_id": None, "application_id": None, "job_id": None},
        )

    def _send_once(self, key: str, state: str, *, target_id: str, link: str, employer: str, title: str, extra: dict) -> dict[str, Any]:
        doc = {
            "_id": new_id("ntf"),
            "idempotency_key": key,
            "state": state,
            "run_id": extra.get("run_id"),
            "application_id": extra.get("application_id"),
            "job_id": extra.get("job_id"),
            "created_at": utcnow(),
            "status": "pending",
        }
        if not self.store.insert_unique("notifications", doc):
            return {"sent": False, "deduped": True, "reason": "already_sent"}
        subject, body, payload = self._compose(state, employer, title, link)
        material = sha256_json({"state": state, "employer": employer, "title": title, "link": link})
        action_payload = {"state": state, "employer": employer, "title": title, "link": link, **{k: v for k, v in extra.items() if k != "run_id" or v}}
        action = self.actions.prepare("owner_notification", target_id, key, action_payload, material)
        outcome = self.actions.execute(action["_id"], "notifier", lambda _doc: self._deliver(subject, body, payload))
        sent = outcome.status == "succeeded"
        self.store.c("notifications").update_one({"_id": doc["_id"]}, {"$set": {"status": "sent" if sent else "failed", "action_id": action["_id"], "updated_at": utcnow()}})
        return {"sent": sent, "deduped": False, "reason": outcome.reason, "action_id": action["_id"]}

    def _compose(self, state: str, employer: str, title: str, link: str) -> tuple[str, str, dict]:
        reason = REASONS[state]
        who = f"{title} at {employer}".strip() if employer else title
        subject = f"{SUBJECTS[state]} — {employer}" if employer else SUBJECTS[state]
        body = f"{who}\n\n{reason}\n\n{link}\n"
        payload = {"event": state, "employer": employer, "title": title, "reason": reason, "url": link}
        return subject, body, payload

    def _deliver(self, subject: str, body: str, payload: dict) -> dict[str, Any]:
        evidence: dict[str, Any] = {"verified": False, "email": "skipped", "webhook": "skipped"}
        if self.email_ready():
            try:
                self._send_email(subject, body)
                evidence["email"] = "sent"
            except Exception as exc:
                evidence["email"] = "failed"
                evidence["email_error"] = type(exc).__name__
                log.warning("email notification failed: %s", type(exc).__name__)
        if self.webhook_ready():
            try:
                self._post_webhook(payload)
                evidence["webhook"] = "sent"
            except Exception as exc:
                evidence["webhook"] = "failed"
                evidence["webhook_error"] = type(exc).__name__
                log.warning("webhook notification failed: %s", type(exc).__name__)
        evidence["verified"] = evidence["email"] == "sent" or evidence["webhook"] == "sent"
        if not evidence["verified"]:
            raise ActionFailed("notification was not delivered", evidence)
        return evidence

    def _send_email(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["To"] = self.settings.notify_email_to
        message["From"] = self.settings.smtp_from or self.settings.smtp_login or self.settings.notify_email_to
        message["Subject"] = subject
        message.set_content(body)
        if self._smtp_send is not None:
            self._smtp_send(message)
            return
        host, port = self.settings.smtp_host, self.settings.smtp_port
        user = self.settings.smtp_login
        password = self.settings.smtp_password.get_secret_value()
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)

    def _post_webhook(self, payload: dict) -> None:
        url = self.settings.notify_webhook_url.strip()
        if self._http_post is not None:
            self._http_post(url, payload)
            return
        import httpx

        response = httpx.post(url, json=payload, timeout=10.0)
        response.raise_for_status()
