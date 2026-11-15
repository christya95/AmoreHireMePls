"""The app's own record of the accounts it creates on job sites, and the dashboard's side of each press.

One row in `site_accounts` per person, site (registrable domain) and account email. The row is what a restart, a
replay or a second task reads before anything is sent: the offer the owner was shown, what they approved, whether
the form was sent, and how it ended. It holds no password, no code and no link. The values on the owner's card (the
account email, the name) are the profile's own and sit in the same private database as the profile.

Status of a row:
  offered            the card is on the application page, waiting for the owner
  item_saved         the login is saved in 1Password, marked unconfirmed; the form has not been sent
  submitting         the form is being sent (written before the final press)
  password_rejected  the site refused the password for its rules; a new one needs the owner's press
  awaiting_email     the form was accepted and the site sent a code or a link
  confirmed          the account works
  exists             the site says this email already has an account; the app never tries again
  refused            the site refused the form for another reason; nothing was created
  unclear            the outcome could not be read; the owner checks the site
  not_created        the owner said the unclear sign-up made no account

The form is sent through `ActionStore.execute` with the row's key as the idempotency key, so it is sent at most once
per approval. Each approval is bound to `offer_hash`: what would be typed, ticked and pressed, the account, and which
password of the item it is. A changed form or a new password needs a new press.

1Password is written only from the owner's press on the dashboard that can reach it. What the run learns later (the
account is confirmed, the outcome is unclear) is kept on the row as `item_pending` and applied at the next press.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from amorehiremepls.core import account_items as items
from amorehiremepls.core.account_items import PasswordRules
from amorehiremepls.core.authorization import ScopeBinding, site_account_key
from amorehiremepls.core.credentials import ApprovalNotGiven, LoginLookupError, LookupTransient, registrable_domain, same_site, secure_page, site_host
from amorehiremepls.core.util import new_id, sha256_json, utcnow

ACTION_KIND = "site_account_create"
OPEN = ("offered", "item_saved", "submitting", "password_rejected")  # nothing is known to exist on the site yet
ITEM_MARKS = ("confirmed", "unclear", "not_created")
APPROVAL_MINUTES = 60  # how long one press covers one send


def offer_hash(material: dict, key: str, number: int) -> str:
    """What one approval is bound to: the plan, the account, and which password of the item would be typed."""
    return sha256_json({"plan": material, "account": key, "password": int(number)})


def rules_dict(rules: PasswordRules | None) -> dict:
    return asdict(rules or PasswordRules())


def rules_from(data: Any) -> PasswordRules:
    names = PasswordRules.__dataclass_fields__
    return PasswordRules(**{name: value for name, value in (data or {}).items() if name in names}) if isinstance(data, dict) else PasswordRules()


def page_address(url: str) -> str:
    """A page's address without its query, which may carry a one-time token."""
    text = str(url or "").strip()
    return text.split("#", 1)[0].split("?", 1)[0][:500] if secure_page(text) else ""


class AccountStore:
    """Rows in `site_accounts`. No secret."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def _rows(self):
        return self.store.c("site_accounts")

    def user_id(self) -> str | None:
        reader = getattr(self.store, "_uid", None)
        return reader() if callable(reader) else None

    def key_for(self, site: str, email: str) -> str:
        return site_account_key(self.user_id(), site, email)

    def get(self, row_id: str) -> dict | None:
        return self._rows().find_one({"_id": row_id}) if row_id else None

    def by_key(self, key: str) -> dict | None:
        return self._rows().find_one({"key": key})

    def for_site(self, site: str) -> dict | None:
        """The newest row for this site's registrable domain, whatever the email."""
        domain = registrable_domain(site_host(site) or site)
        return self._rows().find_one({"domain": domain}, sort=[("updated_at", -1)]) if domain else None

    def open(self, key: str, host: str, signin_url: str, job_id: str = "", application_id: str = "", careers_url: str = "") -> dict:
        """The row for this account, made once. A second task gets the same row."""
        found = self.by_key(key)
        now = utcnow()
        if found is None:
            doc = {
                "_id": new_id("acct"),
                "key": key,
                "host": site_host(host),
                "domain": registrable_domain(host),
                "signin_url": page_address(signin_url),
                "careers_url": page_address(careers_url),
                "status": "offered",
                "passwords_made": 0,
                "answers": {},
                "created_at": now,
                "updated_at": now,
            }
            if not self.store.insert_unique("site_accounts", doc):
                found = self.by_key(key)
            else:
                found = doc
        self._rows().update_one({"_id": found["_id"]}, {"$set": {"job_id": job_id, "application_id": application_id, "host": site_host(host), "updated_at": now}})
        return self.get(found["_id"]) or found

    def claim_send(self, row_id: str, offer: str) -> bool:
        """One sender moves the row from saved to submitting. A second caller gets False and must not send."""
        if not row_id or not offer:
            return False
        now = utcnow()
        result = self._rows().update_one({"_id": row_id, "status": "item_saved", "approved": offer}, {"$set": {"status": "submitting", "sent_at": now, "updated_at": now}})
        return result.modified_count == 1

    def remember_answers(self, row_id: str, answers: dict) -> dict | None:
        """The owner's answers to the card's questions. A security answer is never one of them."""
        clean = {str(key)[:80]: " ".join(str(value).split())[:200] for key, value in answers.items() if str(value).strip()}
        return self.update(row_id, answers=clean)

    def pending_for(self, application_id: str) -> dict | None:
        if not application_id:
            return None
        return self._rows().find_one({"application_id": application_id, "item_pending": {"$in": list(ITEM_MARKS)}}, sort=[("updated_at", -1)])

    def update(self, row_id: str, **fields: Any) -> dict | None:
        if not row_id:
            return None
        self._rows().update_one({"_id": row_id}, {"$set": {**fields, "updated_at": utcnow()}})
        return self.get(row_id)

    def offer(self, row_id: str, card: dict, offer: str, rules: PasswordRules, number: int, step: str, changed: bool = False) -> dict | None:
        """What the owner is being asked to approve right now. An earlier approval for another offer no longer applies."""
        return self.update(row_id, card=card, offer=offer, rules=rules_dict(rules), number=int(number), step=step, changed=bool(changed), approved="")

    def sites(self, limit: int = 40) -> list[dict]:
        """For Settings: `{site, status, item_title, item_pending, at}` newest first."""
        rows = self._rows().find({}, {"domain": 1, "host": 1, "status": 1, "item_title": 1, "item_pending": 1, "updated_at": 1, "application_id": 1}).sort("updated_at", -1)
        return [
            {
                "id": row["_id"],
                "site": row.get("domain") or row.get("host") or "",
                "status": str(row.get("status") or ""),
                "item_title": str(row.get("item_title") or ""),
                "item_pending": str(row.get("item_pending") or ""),
                "at": row.get("updated_at"),
                "application_id": str(row.get("application_id") or ""),
            }
            for row in rows.limit(limit)
        ]


# -- the dashboard's side ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Press:
    """What one press did. `reply` goes to the waiting task; None keeps it waiting."""

    flash: str
    ok: bool
    reply: dict | None = None
    missed: bool = False


def item_for(rt: Any, row: dict, job: dict | None) -> items.ItemSpec | None:
    """The 1Password item this account would be saved as, from the job and this dashboard's own settings."""
    email = rt.account_email()
    url = str(row.get("signin_url") or "") or f"https://{row.get('host') or ''}/"
    try:
        return items.item_spec(job or {}, url, email, row["_id"], vault=rt.settings.site_account_vault, careers_url=str(row.get("careers_url") or ""))
    except items.ItemError:
        return None


def card_view(rt: Any, row: dict | None, job: dict | None) -> dict | None:
    """The approval card for the application page: the plan as the task read it, plus where the login is saved."""
    if not row or not isinstance(row.get("card"), dict):
        return None
    spec = item_for(rt, row, job)
    card = row["card"]
    return {
        **card,
        "id": row["_id"],
        "offer": str(row.get("offer") or ""),
        "step": str(row.get("step") or "account"),
        "number": int(row.get("number") or 1),
        "tries": int(rt.settings.site_account_password_tries),
        "changed": bool(row.get("changed")),
        "said": str(row.get("said") or "")[:300],
        "email": rt.account_email(),
        "item_title": spec.title if spec else "",
        "vault": rt.settings.site_account_vault,
        "terms": [link for link in card.get("terms") or [] if isinstance(link, list) and len(link) == 2 and secure_page(str(link[1]))],
    }


def approve(rt: Any, ask: dict, row: dict, job: dict | None, offer: str) -> Press:
    """The owner pressed Create this account, or Make a new password.

    In order: the approval is saved for exactly this offer, the intent is saved, the login is saved in 1Password
    (unconfirmed) or given a new password, and only then is it sealed to the waiting task. Nothing here sends a form.
    """
    step = str(ask.get("step") or "")
    if not rt.site_account_create_enabled():
        return Press("account_off", False, {"kind": "error", "error": "off"})
    if step not in ("account", "password") or not offer or offer != row.get("offer") or ask.get("offer") != offer or ask.get("account") != row["_id"]:
        return Press("account_changed", False)
    if (row.get("card") or {}).get("state") != "ready":
        return Press("account_answers_needed", False)
    writer, source = rt.item_writer(), rt.credential_source()
    if not writer.available() or not source.available():
        return Press("account_not_here", False)
    if not rt.settings.site_account_vault.strip():
        return Press("account_vault_missing", False)
    spec = item_for(rt, row, job)
    host = site_host(str(ask.get("host") or ""))
    if spec is None or not same_site(host, str(row.get("host") or "")):
        return Press("account_mismatch", False, {"kind": "error", "error": "mismatch"})
    accounts = AccountStore(rt.store)
    key = row["key"]
    existing = rt.actions.by_key(key)
    if step == "password" and existing and existing.get("status") == "failed" and row.get("status") == "password_rejected":
        rt.actions.retire_failed_attempt(existing)  # this approval is a new password, so it may send once
    # The intent and the approval come first. Both are bound to this exact offer.
    payload = {"account": row["_id"], "host": host, "job_id": row.get("job_id"), "application_id": row.get("application_id"), "item_title": spec.title}
    action = rt.actions.prepare(ACTION_KIND, key, key, payload, offer, host=host)
    if action.get("status") != "ready" or action.get("material_hash") != offer:
        return Press("account_already", False, {"kind": "error", "error": "already"})
    if not rt.auth.check(ACTION_KIND, key, offer, host=host).allowed:
        note = f"create an account on {registrable_domain(host)}"
        rt.auth.grant(ACTION_KIND, ScopeBinding(target_id=key, material_hash=offer, allowed_hosts=[host]), ttl=timedelta(minutes=APPROVAL_MINUTES), note=note)
    made = int(row.get("passwords_made") or 0)
    login = None
    if step == "password":
        saved = items.existing_logins(source, host)
        mine = [item for item in saved if items.made_by_app(item) and writer.owns(item, row["_id"])]
        if not mine:
            return Press("account_item_gone", False, {"kind": "error", "error": "not_ours"})
        login = mine[0]
    answer = items.answer_account_ask(
        ask, writer, source, rt.settings.app_secret.get_secret_value(), spec, rules_from(row.get("rules")), login=login, made=made, tries=rt.settings.site_account_password_tries
    )
    if answer.missed:
        return Press(answer.flash, False, missed=True)
    if not answer.ok:
        # Nothing will be sent for this approval. It is withdrawn so it cannot be used later.
        decision = rt.auth.check(ACTION_KIND, key, offer, host=host)
        if decision.allowed and decision.scope_id:
            rt.auth.revoke(decision.scope_id, answer.flash)
        if answer.made:
            accounts.update(row["_id"], passwords_made=answer.made, item_title=answer.title or spec.title)
        return Press(answer.flash, False, answer.reply)
    accounts.update(
        row["_id"], status="item_saved", approved=offer, passwords_made=answer.made, item_title=answer.title or spec.title, vault=rt.settings.site_account_vault, said=""
    )
    return Press(answer.flash, True, answer.reply)


def apply_item_mark(rt: Any, row: dict) -> str:
    """Bring the 1Password item in line with what the run learned. Called only from a press. Returns a flash key."""
    mark = str(row.get("item_pending") or "")
    if mark not in ITEM_MARKS:
        return "account_item_nothing"
    writer, source = rt.item_writer(), rt.credential_source()
    if not writer.available() or not source.available():
        return "account_not_here"
    accounts = AccountStore(rt.store)
    try:
        mine = [login for login in items.existing_logins(source, str(row.get("host") or "")) if items.made_by_app(login) and writer.owns(login, row["_id"])]
        if not mine:
            accounts.update(row["_id"], item_pending="")
            return "account_item_gone"
        if mark == "confirmed":
            items.mark_confirmed(writer, mine[0], row["_id"])
        elif mark == "unclear":
            items.mark_unclear(writer, mine[0], row["_id"])
        else:
            writer.archive(mine[0], row["_id"])  # the app's own unused login goes to 1Password's Archive, never a delete
    except ApprovalNotGiven:
        return "account_not_approved"
    except LookupTransient:
        return "account_save_failed"
    except (items.ItemError, LoginLookupError):
        return "account_cannot_save"
    accounts.update(row["_id"], item_pending="", item_state=mark)
    return {"confirmed": "account_item_confirmed", "unclear": "account_item_unclear", "not_created": "account_item_archived"}[mark]


def resolve_unclear(rt: Any, row: dict, exists: bool) -> str:
    """The owner looked at the site after an unclear sign-up. Only they can say whether the account exists."""
    key = row["key"]
    action = rt.actions.by_key(key)
    if row.get("status") != "unclear" or not action or action.get("status") != "needs_verification":
        return "account_resolve_missing"
    rt.actions.reconcile(action["_id"], "succeeded" if exists else "failed", {"resolved_by": "owner", "exists": bool(exists)}, by="owner")
    mark = "confirmed" if exists else "not_created"
    AccountStore(rt.store).update(row["_id"], status=mark, item_pending=mark, confirmed_at=utcnow() if exists else None)
    return "account_resolved_exists" if exists else "account_resolved_none"
