"""Environment-driven configuration with validation and safe local defaults."""

from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application ----------------------------------------------------------
    app_env: str = "local"
    log_level: str = "INFO"
    api_key: str = Field(default="", description="Master API key for X-API-Key auth")
    cors_origins: str = "*"
    rate_limit: str = "60/minute"

    # PostgreSQL -----------------------------------------------------------
    pghost: str = "localhost"
    pgport: int = 5432
    pgdatabase: str = "eval_platform"
    pguser: str = "evalforge"
    pgpassword: str = ""
    pgschema: str = "public"
    database_url: str = ""
    test_pgdatabase: str = "eval_platform_test"

    db_pool_min_size: int = 1
    db_pool_max_size: int = 20
    db_connect_timeout: int = 10

    # Temporary dataset input (folder-per-dataset_id). Not a durable store.
    evaluation_temp_root: str = "./temp"

    # Runner ---------------------------------------------------------------
    runner_poll_interval_seconds: float = 2.0
    runner_claim_batch_size: int = 5
    runner_max_concurrency: int = 4
    runner_lease_seconds: int = 300
    runner_ticket_timeout_seconds: float = 120.0
    runner_max_attempts: int = 3
    runner_retry_backoff_seconds: str = "30,60,120"
    runner_recovery_interval_seconds: float = 60.0
    runner_shutdown_grace_seconds: float = 180.0

    # LLM providers --------------------------------------------------------
    default_provider: str = "openai"
    default_model: str = "gpt-4o-mini"
    judge_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 120.0
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    # Evaluation defaults --------------------------------------------------
    default_on_missing_context: str = "not_applicable"
    semantic_threshold: float = 0.75
    embedding_provider: str = "hash"
    embedding_model: str = "text-embedding-3-small"
    input_cost_per_1m: float = 0.15
    output_cost_per_1m: float = 0.60

    @field_validator("pgschema")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        # The schema name is interpolated into SET search_path / CREATE SCHEMA,
        # which cannot be parameterized, so it must be a plain identifier.
        if not IDENTIFIER_RE.match(value):
            raise ValueError(f"PGSCHEMA must be a plain SQL identifier, got {value!r}")
        return value

    @field_validator("default_on_missing_context")
    @classmethod
    def _validate_missing_context(cls, value: str) -> str:
        allowed = {"not_applicable", "fail"}
        lowered = value.strip().lower()
        if lowered not in allowed:
            raise ValueError(f"DEFAULT_ON_MISSING_CONTEXT must be one of {sorted(allowed)}")
        return lowered

    @model_validator(mode="after")
    def _validate_pool_and_runner(self) -> Settings:
        if self.db_pool_min_size < 0 or self.db_pool_max_size < 1:
            raise ValueError("DB pool sizes must be positive")
        if self.db_pool_max_size < self.db_pool_min_size:
            raise ValueError("DB_POOL_MAX_SIZE must be >= DB_POOL_MIN_SIZE")
        if self.runner_max_concurrency < 1:
            raise ValueError("RUNNER_MAX_CONCURRENCY must be >= 1")
        if self.runner_claim_batch_size < 1:
            raise ValueError("RUNNER_CLAIM_BATCH_SIZE must be >= 1")
        if self.runner_max_attempts < 1:
            raise ValueError("RUNNER_MAX_ATTEMPTS must be >= 1")
        if self.runner_lease_seconds < 1:
            raise ValueError("RUNNER_LEASE_SECONDS must be >= 1")
        if not self.retry_backoff_seconds:
            raise ValueError("RUNNER_RETRY_BACKOFF_SECONDS must contain at least one value")
        return self

    @property
    def is_development(self) -> bool:
        return self.app_env in {"development", "dev", "local", "test"}

    @property
    def conninfo(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"host={self.pghost} port={self.pgport} dbname={self.pgdatabase} "
            f"user={self.pguser} password={self.pgpassword} "
            f"connect_timeout={self.db_connect_timeout}"
        )

    @property
    def retry_backoff_seconds(self) -> list[float]:
        parts = [p.strip() for p in self.runner_retry_backoff_seconds.split(",") if p.strip()]
        try:
            return [float(p) for p in parts]
        except ValueError as exc:  # pragma: no cover - configuration error path
            raise ValueError(
                "RUNNER_RETRY_BACKOFF_SECONDS must be a comma separated number list"
            ) from exc

    def backoff_for_attempt(self, attempt_count: int) -> float:
        """Backoff for the attempt that just failed (1-based)."""
        delays = self.retry_backoff_seconds
        index = min(max(attempt_count - 1, 0), len(delays) - 1)
        return delays[index]


settings = Settings()
