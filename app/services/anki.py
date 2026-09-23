from __future__ import annotations

import hashlib
import re
from pathlib import Path

import genanki

from .media import extract_audio

MODEL_ID = 1918042702

MODEL = genanki.Model(
    MODEL_ID,
    "LexiQuest Word in Context",
    fields=[
        {"name": "SourceWord"},
        {"name": "TargetWord"},
        {"name": "SourcePhrase"},
        {"name": "TargetPhrase"},
        {"name": "Audio"},
        {"name": "VideoId"},
        {"name": "PhraseId"},
    ],
    templates=[
        {
            "name": "Word",
            "qfmt": """
<div class="word source-word">{{SourceWord}}</div>
<div class="context source-context">{{SourcePhrase}}</div>
<div class="audio">{{Audio}}</div>
""",
            "afmt": """
{{FrontSide}}
<hr>
<div class="word target-word">{{TargetWord}}</div>
<div class="context target-context">{{TargetPhrase}}</div>
""",
        }
    ],
    css="""
.card { font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size: 20px; text-align: center; color: #111; background: #fff; padding: 12px; }
.word { font-size: 34px; font-weight: 800; margin: 16px 0; }
.target-word { color: #563fd8; }
.context { font-size: 19px; line-height: 1.45; margin: 16px auto; max-width: 720px; }
.target-context { color: #555; }
.audio { margin-top: 14px; }
hr { border: 0; border-top: 1px solid #ddd; margin: 22px 0; }
""",
)


def stable_int(value: str) -> int:
    return int(hashlib.sha256(value.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def _norm_word(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def build_selected_deck(video, cards, phrases_by_id: dict[int, object], video_path: Path, audio_dir: Path, output_path: Path) -> Path:
    deck_id = stable_int(f"lexiquest:{video.id}")
    deck = genanki.Deck(deck_id, f"LexiQuest::{video.title}")
    media_files: set[str] = set()

    for card in cards:
        phrase = phrases_by_id.get(card.phrase_id)
        if not phrase or not card.source_word or not card.target_word:
            continue

        source_phrase = card.source_phrase_snapshot or phrase.source_text
        target_phrase = card.target_phrase_snapshot if card.target_phrase_snapshot is not None else (phrase.translated_text or "")
        clip_start = float(card.clip_start if card.clip_start is not None else phrase.start_time)
        clip_end = float(card.clip_end if card.clip_end is not None else phrase.end_time)
        if clip_end <= clip_start:
            clip_start = float(phrase.start_time)
            clip_end = float(phrase.end_time)

        clip_name = f"lq_{video.id}_{card.id or phrase.id}_{int(clip_start*1000)}.mp3"
        clip_path = audio_dir / clip_name
        if not clip_path.exists():
            extract_audio(video_path, clip_start, clip_end, clip_path)
        media_files.add(str(clip_path))

        # Stable across phrase-row replacement: the saved-word row is the identity.
        guid = genanki.guid_for(video.id, str(card.id or int(clip_start * 1000)), _norm_word(card.source_word))
        note = genanki.Note(
            model=MODEL,
            fields=[
                card.source_word,
                card.target_word,
                source_phrase,
                target_phrase,
                f"[sound:{clip_name}]",
                video.id,
                str(card.phrase_id),
            ],
            guid=guid,
        )
        deck.add_note(note)

    package = genanki.Package(deck)
    package.media_files = sorted(media_files)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    package.write_to_file(str(output_path))
    return output_path
