"""Application settings, loaded from environment variables (or a local .env file).

Nothing secret has a default: the app refuses to start without DATABASE_URL and
a JWT_SECRET_KEY of at least 32 characters.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Multi-Branch Inventory Engine"
    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str
    db_pool_size: int = 10
    db_max_overflow: int = 20

    jwt_secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=30, gt=0, le=24 * 60)
    # bcrypt work factor. 12 is a sensible production default; tests lower it for speed.
    bcrypt_rounds: int = Field(default=12, ge=4, le=16)

    # Only used by scripts/seed.py to give demo users a password.
    seed_default_password: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
