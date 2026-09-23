from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_user: str = "lexi"
    app_password: str = "change-me"
    media_root: Path = Path("/app/media")
    data_dir: Path = Path("/app/data")
    dictionary_dir: Path = Path("/app/dictionaries")
    export_dir: Path = Path("/app/exports")
    max_upload_mb: int = 2048
    max_dictionary_upload_mb: int = 1024
    vot_bridge_url: str = "http://vot:3100"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lexiquest.db"


settings = Settings()
for path in [
    settings.media_root,
    settings.media_root / "videos",
    settings.media_root / "audio",
    settings.media_root / "tmp",
    settings.data_dir,
    settings.dictionary_dir,
    settings.export_dir,
]:
    path.mkdir(parents=True, exist_ok=True)
