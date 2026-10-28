from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "anthropic/claude-sonnet-5.5"

    # Name the chat UI shows in its model picker
    agent_model_name: str = "travel-agent"

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

    # LangSmith tracing (https://smith.langchain.com); optional, independent of Langfuse
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "travel-agent"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # Raw guidebook PDFs (local only, gitignored) and their extracted markdown
    reference_dir: Path = Path("reference")
    guidebooks_dir: Path = Path("data/guidebooks")


@lru_cache
def get_settings() -> Settings:
    return Settings()
