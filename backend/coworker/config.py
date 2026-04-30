from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration loaded from environment variables.

    Environment variables override the .env file.
    Production env file is age-encrypted and decrypted at systemd LoadCredential time.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="forbid",
    )

    # Environment
    ENVIRONMENT: Literal["dev", "staging", "production"] = "dev"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Database
    DATABASE_URL: PostgresDsn
    DATABASE_POOL_SIZE: int = 20
    DATABASE_POOL_MAX_OVERFLOW: int = 10

    # Redis
    REDIS_URL: RedisDsn

    # Anthropic
    ANTHROPIC_API_KEY: SecretStr
    ANTHROPIC_MODEL_DEFAULT: str = "claude-sonnet-4-5"
    ANTHROPIC_MODEL_REASONING: str = "claude-opus-4-5"
    ANTHROPIC_MODEL_FAST: str = "claude-haiku-4-5"
    ANTHROPIC_MAX_TOKENS_DEFAULT: int = 8192
    ANTHROPIC_EXTENDED_THINKING_BUDGET: int = 16000

    # Microsoft (per-firm tenancy means these are the *fallback* MC&S app
    # credentials for development only; real auth is per-firm in DB)
    MS_CLIENT_ID_FALLBACK: str = ""
    MS_CLIENT_SECRET_FALLBACK: SecretStr = SecretStr("")
    MS_TENANT_ID_FALLBACK: str = ""

    # Embedding provider
    EMBEDDING_PROVIDER: Literal["voyage", "openai"] = "voyage"
    VOYAGE_API_KEY: SecretStr | None = None
    OPENAI_API_KEY: SecretStr | None = None

    # Encryption
    MASTER_ENCRYPTION_KEY: SecretStr  # 32 bytes base64; envelope encryption root

    # Audit
    AUDIT_LOG_GENESIS_HASH: str = "0" * 64

    # Shadow mode (must be False to write to external systems)
    SHADOW_MODE: bool = True
    SHADOW_MODE_OVERRIDE_FIRMS: list[str] = []

    # Rate limits
    OUTBOUND_RATE_PER_MINUTE_PER_PLUGIN: int = 5
    OUTBOUND_RATE_PER_HOUR_PER_MAILBOX: int = 50
    OUTBOUND_RATE_PER_DAY_PER_MAILBOX: int = 200

    # Webhook validation
    GRAPH_WEBHOOK_CLIENT_STATE: SecretStr = SecretStr("")  # HMAC key for Graph notifications

    # Backups
    SPACES_REGION: str = "syd1"
    SPACES_BUCKET: str = "coworker-v3-backups-syd1"
    SPACES_ACCESS_KEY: SecretStr | None = None
    SPACES_SECRET_KEY: SecretStr | None = None

    # External monitoring
    GLITCHTIP_DSN: str | None = None

    # Confidence
    DEFAULT_AUTO_APPROVE_THRESHOLD: float = 0.85
    SELF_CONSISTENCY_SAMPLES: int = 5

    # Two-person approval categories
    TWO_PERSON_REQUIRED_CATEGORIES: list[str] = Field(
        default=[
            "engagement_letter",
            "formal_demand",
            "fusesign_envelope_new_client",
            "memory_purge",
        ]
    )

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
