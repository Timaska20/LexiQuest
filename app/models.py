from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Video(SQLModel, table=True):
    id: str = Field(primary_key=True, index=True)
    source_url: Optional[str] = None
    source_provider: str = "upload"
    title: str
    source_lang: str = Field(index=True)
    target_lang: str = Field(index=True)
    status: str = Field(default="queued", index=True)
    file_path: Optional[str] = None
    file_size_bytes: Optional[int] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Phrase(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    video_id: str = Field(foreign_key="video.id", index=True)
    start_time: float
    end_time: float
    source_text: str
    translated_text: str = ""
    provider: str = Field(default="source", index=True)
    order_index: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Flashcard(SQLModel, table=True):
    """A word intentionally saved by the learner from one subtitle phrase."""

    id: Optional[int] = Field(default=None, primary_key=True)
    phrase_id: int = Field(foreign_key="phrase.id", index=True)
    source_word: Optional[str] = Field(default=None, index=True)
    # target_word is kept for backwards-compatible SQLite migrations. It is the
    # dictionary translation/meaning selected by the learner.
    target_word: Optional[str] = None
    dictionary_name: Optional[str] = None
    # Snapshot the exact learning context at save/sync time. An Anki card should
    # not silently change because subtitle rows are later re-segmented.
    source_phrase_snapshot: Optional[str] = None
    target_phrase_snapshot: Optional[str] = None
    clip_start: Optional[float] = None
    clip_end: Optional[float] = None
    audio_path: Optional[str] = None
    exported_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    video_id: str = Field(foreign_key="video.id", index=True)
    kind: str = "ingest"
    status: str = Field(default="queued", index=True)
    progress: int = 0
    message: str = "Queued"
    payload_json: str = "{}"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Dictionary(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    format: str = "stardict"
    source_lang: str = Field(index=True)
    target_lang: str = Field(index=True)
    base_path: str
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)
