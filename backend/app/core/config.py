import os
from typing import Literal, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

_current_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.dirname(os.path.dirname(_current_dir))
_env_file_path = os.path.join(_backend_dir, ".env")

class Settings(BaseSettings):
    APP_ENV: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: str = "INFO"
    MAX_CONCURRENCY_LIMIT: int = 3
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None

    # Pydantic V2 Settings configuration
    model_config = SettingsConfigDict(
        env_file=_env_file_path,
        env_file_encoding="utf-8",
        extra="ignore"
    )

# Instantiate settings for global import
settings = Settings()
