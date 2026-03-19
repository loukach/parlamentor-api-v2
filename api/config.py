"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings


def _normalize_async_url(url: str) -> str:
    """Rewrite postgres:// and postgresql:// to postgresql+asyncpg://."""
    if url.startswith("postgresql+asyncpg://"):
        return url  # already correct
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # App database (read-write)
    database_url: str = "postgresql+asyncpg://localhost:5432/parlamentor"

    # Parla! database (read-only, parliamentary data)
    parla_database_url: str = "postgresql+asyncpg://localhost:5432/viriato"

    # Anthropic
    anthropic_api_key: str = ""

    # Langfuse observability
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # Stage-specific model overrides (optional)
    analysis_model: str = ""  # Defaults to Sonnet 4.6 if not set
    drafting_model: str = ""  # Defaults to Opus 4.6 if not set

    # Nhost
    nhost_subdomain: str = ""
    nhost_region: str = "eu-central-1"

    # CORS
    cors_origins: str = "http://localhost:5180,https://parlamentor-web.onrender.com"

    @property
    def app_db_url(self) -> str:
        return _normalize_async_url(self.database_url)

    @property
    def parla_db_url(self) -> str:
        return _normalize_async_url(self.parla_database_url)


settings = Settings()

DEFAULT_MODEL = "claude-sonnet-4-6"
