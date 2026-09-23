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
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    google_oauth_scopes: str = "https://www.googleapis.com/auth/spreadsheets openid email"
    google_progress_sheet_id: str = "17Sfv37TCQx8Fw09c_8CGwzUG6iB2a-ZSY8rqVgP8GtM"
    google_analytics_sheet_id: str = "1o3ftf_R3K67meRjqA_6TgIo9W4wl0PfiM2bn8jjaeRo"
    google_progress_tab: str = "Daily_Progress"
    google_analytics_tab: str = "Weak_Spots_Log"
    google_timezone: str = "Asia/Almaty"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lexiquest.db"

    @property
    def google_tokens_path(self) -> Path:
        return self.data_dir / "google_tokens.json"


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
