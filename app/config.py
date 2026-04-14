from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "ROM Finder"
    host: str = "127.0.0.1"
    port: int = 8080
    db_url: str = "sqlite:///./rom_finder.db"
    debug: bool = False

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
