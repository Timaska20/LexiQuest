from typing import Optional
from pydantic import BaseModel, Field, field_validator

from .languages import normalize_lang


class VideoURLCreate(BaseModel):
    url: str = Field(min_length=4, max_length=4096)
    title: Optional[str] = Field(default=None, max_length=300)
    source_lang: str = "en"
    target_lang: str = "ru"

    @field_validator("source_lang", "target_lang")
    @classmethod
    def validate_lang(cls, value: str) -> str:
        value = normalize_lang(value)
        if not value or len(value) > 16 or not all(c.isalnum() or c in "-_" for c in value):
            raise ValueError("Invalid language code")
        return value


class VOTSubtitleRequest(BaseModel):
    source_lang: str = "auto"
    target_lang: str = "ru"

    @field_validator("source_lang", "target_lang")
    @classmethod
    def validate_vot_lang(cls, value: str) -> str:
        value = normalize_lang(value, "auto")
        if not value or len(value) > 16 or not all(c.isalnum() or c in "-_" for c in value):
            raise ValueError("Invalid language code")
        return value


class PhraseCreate(BaseModel):
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    source_text: str = Field(min_length=1, max_length=4000)
    translated_text: str = Field(default="", max_length=4000)
    order_index: Optional[int] = None


class PhraseUpdate(BaseModel):
    start_time: Optional[float] = Field(default=None, ge=0)
    end_time: Optional[float] = Field(default=None, gt=0)
    source_text: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    translated_text: Optional[str] = Field(default=None, max_length=4000)
    order_index: Optional[int] = None


class FlashcardCreate(BaseModel):
    source_word: str = Field(min_length=1, max_length=200)
    target_word: str = Field(min_length=1, max_length=2000)
    dictionary_name: Optional[str] = Field(default=None, max_length=300)
