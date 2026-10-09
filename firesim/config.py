from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    model: str = Field(default="gpt-6.1-sol", validation_alias="FIRESIM_MODEL")
    reasoning_effort: str = Field(default="medium", validation_alias="FIRESIM_REASONING_EFFORT")
    max_frames: int = Field(default=24, validation_alias="FIRESIM_MAX_FRAMES")
    frame_size: int = Field(default=1024, validation_alias="FIRESIM_FRAME_SIZE")
    data_dir: Path = Field(default=Path("data"), validation_alias="FIRESIM_DATA_DIR")
    max_upload_mb: int = Field(default=500, validation_alias="FIRESIM_MAX_UPLOAD_MB")


def get_settings() -> Settings:
    # Not cached: .env is re-read per request so a newly added key works without a restart.
    return Settings()
