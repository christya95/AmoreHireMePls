"""Runtime configuration. Non-secret defaults live in .env.example; secrets arrive via environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AMORE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

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

    model: str = ""  # "openai:gpt-..." or "anthropic:claude-..."; empty = no provider configured
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

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""

    discovery_schedule: str = ""
    langsmith_tracing: bool = False

    @property
    def allowed_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.browser_allowed_hosts.split(",") if h.strip()}

    @property
    def provider_configured(self) -> bool:
        return bool(self.model)

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
