from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-6-luna"
    # Retries on 429/5xx with exponential backoff (honours Retry-After); 10 rides out ~1 minute,
    # the window of OpenRouter's per-minute limits
    llm_max_retries: int = 10
    # Optional client-side pacing for rate-limited accounts, e.g. 18 for a 20 req/min limit
    llm_requests_per_minute: float | None = None

    # Model id the chat UI selects (stable; referenced by DEFAULT_MODELS in compose) and the
    # display name it shows
    agent_model_name: str = "travel-agent"
    agent_display_name: str = "AskTravel"

    host: str = "0.0.0.0"
    port: int = 8000

    # Checkpointer (thread memory) and store (long-term memory) both live here
    database_url: str = "postgresql://travel:travel@localhost:5433/travel_agent"

    # Langfuse tracing (self-hosted via docker compose, UI on :3001). Keys match the project
    # docker/langfuse.compose.yml bootstraps, so this works out of the box locally.
    langfuse_tracing: bool = False
    langfuse_public_key: str = "pk-lf-travel-agent-dev"
    langfuse_secret_key: SecretStr = SecretStr("sk-lf-travel-agent-dev")
    langfuse_host: str = "http://localhost:3001"

    # Raw guidebook PDFs (local only, gitignored) and their extracted markdown
    reference_dir: Path = Path("reference")
    guidebooks_dir: Path = Path("data/guidebooks")


@lru_cache
def get_settings() -> Settings:
    return Settings()
