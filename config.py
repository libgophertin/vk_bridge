"""Загрузка конфигурации из .env через pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения, читаются из переменных окружения / .env."""

    vk_token: str
    tg_bot_token: str
    tg_owner_id: int

    # Путь к файлу SQLite. В docker переопределяется на /data/bridge.db
    db_path: str = "bridge.db"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()  # type: ignore[call-arg]
