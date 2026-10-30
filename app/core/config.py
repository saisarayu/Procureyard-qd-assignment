"""Core configuration loaded from environment variables."""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@db:5432/quickdrop"
    DATABASE_URL_SYNC: str = "postgresql://postgres:postgres@db:5432/quickdrop"

    # PaySwift
    PAYSWIFT_BASE_URL: str = "http://payswift:8081"

    # LLM
    LLM_PROVIDER: str = "groq"
    GROQ_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # App
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "info"

    # Ops auth
    OPS_USERNAME: str = "ops"
    OPS_PASSWORD: str = "quickdrop2026"

    # Financial limits
    AUTO_PAY_MAX_AMOUNT: int = 200
    AUTO_PAY_MAX_PER_DAY: int = 1

    # Dispute window in days
    DISPUTE_WINDOW_DAYS: int = 7

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
