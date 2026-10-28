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

    # Raw guidebook PDFs (local only, gitignored) and their extracted markdown
    reference_dir: Path = Path("reference")
    guidebooks_dir: Path = Path("data/guidebooks")


@lru_cache
def get_settings() -> Settings:
    return Settings()
