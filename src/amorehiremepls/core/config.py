"""Runtime configuration. Non-secret defaults live in .env.example; secrets arrive via environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AMORE_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mongodb_uri: str = "mongodb://127.0.0.1:27027"
    mongodb_database: str = "amorehiremepls"
    mongodb_username: str = ""
    mongodb_password: SecretStr = SecretStr("")
    mongodb_auth_source: str = "admin"

    bind_host: str = "127.0.0.1"
    port: int = 8790
    app_password: SecretStr = SecretStr("")
    app_secret: SecretStr = SecretStr("")
    secure_cookies: bool = False
    # The candidate's time zone (IANA name). Drives the dashboard clock, greeting, application dates and the
    # discovery schedule. Set it in .env; the deploy compose files carry each operator's value.
    timezone: str = "UTC"

    artifact_dir: Path = Path("./runtime/artifacts")
    private_dir: Path = Path("./runtime/private")
    log_dir: Path = Path("./runtime/logs")
    # Drop folder for the candidate's own files: one base resume DOCX plus extra career notes (.md/.txt/.docx).
    # Read at tailoring and fit-ranking time; only its README is tracked in git. Missing or empty = current behavior.
    candidate_dir: Path = Path("./candidate")
    # Character budget for the extra documents' text sent as evidence (files in name order, cut at the limit).
    candidate_supplemental_max_chars: int = Field(default=12_000, ge=0, le=60_000)

    # Default provider is OpenAI. AMORE_MODEL may be "gpt-..." (uses AMORE_MODEL_PROVIDER)
    # or fully qualified "anthropic:claude-..." to select another provider. Empty = no provider configured.
    model: str = ""
    model_provider: str = "openai"
    pricing_file: Path = Path("./config/pricing.json")

    budget_max_tokens: int = 200_000
    budget_max_usd: float = 2.0
    budget_max_tool_steps: int = 60
    budget_max_seconds: int = 1800
    worker_concurrency: int = Field(default=1, ge=1, le=4)

    # Optional private GitHub application ledger. Off by default: confirmed applications are then recorded in the
    # database only and the tracker queue marks them "not applicable". On: a local clone of AMORE_TRACKER_REMOTE_URL
    # (its `records.py` script and `data/applications.json`) receives one scoped commit per confirmed application.
    tracker_enabled: bool = False
    tracker_remote_url: str = ""
    tracker_clone_dir: Path = Path("./runtime/private/tracker")
    tracker_remote: str = "origin"
    tracker_branch: str = "main"
    tracker_push_enabled: bool = False

    browser_headless: bool = True
    browser_session_dir: Path = Path("./runtime/private/sessions")
    browser_allowed_hosts: str = ""
    # Extra hosts that may serve scripts/styles/fonts/frames to an allowed page (never navigation). Site adapters add
    # their own (Greenhouse: its CDN and the reCAPTCHA hosts); list more here only if a board breaks without them.
    browser_resource_hosts: str = ""

    # Automatic CAPTCHA step. Off by default: a human check pauses the application for the owner. On: simple
    # text/image-grid challenges on the owner's own approved submission are sent to the configured model, at most N
    # attempts, all recorded. Turning it on is the operator's explicit acceptance of the platform-terms risk (the
    # original operator accepted it on 2026-11-05). Overridable from Settings (control `captcha_autosolve`).
    captcha_autosolve: bool = False
    captcha_max_attempts: int = Field(default=3, ge=1, le=5)
    # How long the worker keeps the browser open after a human check so Continue can resume the same page.
    # 0 turns the hold off (immediate handoff). The worker is single-browser, so other jobs wait during a hold.
    human_check_hold_minutes: int = Field(default=30, ge=0, le=180)
    # Localhost-only Chromium debug port while a hold is active. 0 disables it. Not reachable on the NAS.
    human_check_cdp_port: int = Field(default=9222, ge=0, le=65535)
    # Sign in on a job site with a login the owner saved in 1Password. Off by default: a sign-in page stops the
    # application for the owner. On: the application page asks the owner to allow 1Password each time, the dashboard
    # that received the press reads the login (1Password shows its own Touch ID or password window there), and the
    # login reaches the browser task sealed to a one-time key. A refused password is never typed twice, no account is
    # ever created, and nothing is submitted without a matching approval. Overridable from Settings (control `site_signin`).
    site_signin: bool = False
    # Optional: the 1Password account (sign-in address or id) and vault to search. Empty searches everything readable.
    site_signin_account: str = ""
    site_signin_vault: str = ""
    # Ceilings. Page work for one sign-in, the wait after the password or code is sent, and how many codes may be typed.
    site_signin_max_seconds: int = Field(default=240, ge=30, le=900)
    site_signin_settle_seconds: int = Field(default=30, ge=5, le=120)
    site_signin_code_attempts: int = Field(default=3, ge=1, le=5)
    # Create an account on a job site that needs one when 1Password has no login for it. Off by default: the run stops
    # and the owner creates the account. On: one approval per site (its own scope, never implied by an approval to
    # apply); the login is saved in 1Password, with a password 1Password generates, before the sign-up form is sent
    # once. Needs `site_signin`. Overridable from Settings (control `site_account_create`).
    site_account_create: bool = False
    # The 1Password vault that receives logins the app creates. Empty = 1Password's own default vault. Use a vault the
    # deployment's service account cannot read, so a new password is never readable at rest from another computer.
    site_account_vault: str = ""
    # The address new accounts are registered with. Empty = the profile's email.
    site_account_email: str = ""
    # Passwords one sign-up may use in all: the first, plus a new one each time the site refuses it for its rules.
    site_account_password_tries: int = Field(default=3, ge=1, le=3)
    # Read the verification email a job site sends after a sign-up (read-only mail access, connected with
    # `amore mail connect` on the computer that runs the dashboard). Off by default: the owner types the code or opens
    # the link. Overridable from Settings (control `mail_verification`).
    mail_verification: bool = False
    # How long a verification email is waited for before the owner is asked instead.
    mail_wait_seconds: int = Field(default=300, ge=30, le=900)
    # Where the mail grant is kept: "1password" (one item in `site_account_vault`, read at the owner's press and held
    # in memory) or "file" (owner-only files under the private folder's `mail/`, left out of backups).
    mail_grant_store: Literal["1password", "file"] = "1password"
    # When an attempt stops short of a confirmed success, keep what the page showed and everything it sent and
    # received, unmasked, in the job's private artifact folder until the next attempt. It can hold a site password.
    stop_record: bool = True
    stop_record_trace: bool = True
    # One fresh-session try after a human check, before the owner is notified. 0 sends the note immediately.
    # Skipped while a hold is still open. The later try never clicks submit if the first try already did.
    challenge_retry_minutes: int = Field(default=180, ge=0, le=1440)

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""  # AMORE_SMTP_USERNAME, older name for the same login
    smtp_user: str = ""  # AMORE_SMTP_USER, preferred; wins over smtp_username when both are set
    smtp_password: SecretStr = SecretStr("")  # injected from 1Password; never written to .env
    smtp_from: str = ""
    notify_email_to: str = ""
    notify_webhook_url: str = ""
    publish_url: str = ""  # full dashboard origin, e.g. http://192.168.2.49:8790
    publish_host: str = ""  # used when publish_url is empty (NAS stack bind address)

    discovery_schedule: str = ""
    langsmith_tracing: bool = False

    @property
    def allowed_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.browser_allowed_hosts.split(",") if h.strip()}

    @property
    def resource_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.browser_resource_hosts.split(",") if h.strip()}

    @property
    def effective_model(self) -> str:
        """'provider:model' string for LangChain; empty when no model is configured."""
        if not self.model:
            return ""
        return self.model if ":" in self.model else f"{self.model_provider}:{self.model}"

    @property
    def provider_configured(self) -> bool:
        return bool(self.effective_model)

    @property
    def smtp_login(self) -> str:
        """Mailbox login. `AMORE_SMTP_USER` wins; `AMORE_SMTP_USERNAME` remains for older outreach config."""
        return self.smtp_user or self.smtp_username

    def dashboard_base_url(self) -> str:
        """Origin for links in notifications. `AMORE_PUBLISH_URL` wins, else `AMORE_PUBLISH_HOST` plus the port."""
        explicit = (self.publish_url or "").strip().rstrip("/")
        if explicit:
            return explicit
        host = (self.publish_host or "").strip()
        if not host or host in {"0.0.0.0", "::", "[::]"}:
            host = self.bind_host or "127.0.0.1"
        return f"http://{host}:{self.port}"

    def mongo_client_kwargs(self) -> dict:
        kwargs: dict = {"serverSelectionTimeoutMS": 5000, "appname": "amorehiremepls"}
        if self.mongodb_username:
            kwargs.update(
                username=self.mongodb_username,
                password=self.mongodb_password.get_secret_value(),
                authSource=self.mongodb_auth_source,
            )
        return kwargs


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
