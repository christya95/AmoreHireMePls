"""Runtime configuration. Non-secret defaults live in .env.example; secrets arrive via environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    timezone: str = "America/Toronto"

    artifact_dir: Path = Path("./runtime/artifacts")
    private_dir: Path = Path("./runtime/private")
    log_dir: Path = Path("./runtime/logs")

    # Default provider is OpenAI (Joss, 2026-11-04). AMORE_MODEL may be "gpt-..." (uses AMORE_MODEL_PROVIDER)
    # or fully qualified "anthropic:claude-..." to select another provider. Empty = no provider configured.
    model: str = ""
    model_provider: str = "openai"
    pricing_file: Path = Path("./config/pricing.json")

    budget_max_tokens: int = 200_000
    budget_max_usd: float = 2.0
    budget_max_tool_steps: int = 60
    budget_max_seconds: int = 1800
    worker_concurrency: int = Field(default=1, ge=1, le=4)

    tracker_clone_dir: Path = Path("./runtime/private/job-application-records")
    tracker_remote: str = "origin"
    tracker_branch: str = "main"
    tracker_push_enabled: bool = False

    browser_headless: bool = True
    browser_session_dir: Path = Path("./runtime/private/sessions")
    browser_allowed_hosts: str = ""
    # Extra hosts that may serve scripts/styles/fonts/frames to an allowed page (never navigation). Site adapters add
    # their own (Greenhouse: its CDN and the reCAPTCHA hosts); list more here only if a board breaks without them.
    browser_resource_hosts: str = ""

    # Automatic CAPTCHA step. Off by default: a human check pauses the application for Joss. On: simple text/image-grid
    # challenges on the user's own approved submission are sent to the configured model, at most N attempts, all
    # recorded. Joss accepted the platform-terms risk on 2026-11-05. Overridable from Settings (control `captcha_autosolve`).
    captcha_autosolve: bool = False
    captcha_max_attempts: int = Field(default=3, ge=1, le=5)
    # How long the worker keeps the browser open after a human check so Continue can resume the same page.
    # 0 turns the hold off (immediate handoff). The worker is single-browser, so other jobs wait during a hold.
    human_check_hold_minutes: int = Field(default=30, ge=0, le=180)
    # Localhost-only Chromium debug port while a hold is active. 0 disables it. Not reachable on the NAS.
    human_check_cdp_port: int = Field(default=9222, ge=0, le=65535)

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
