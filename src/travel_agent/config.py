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

    data_dir: Path = Path("data")


@lru_cache
def get_settings() -> Settings:
    return Settings()
