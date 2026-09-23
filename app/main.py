from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from .auth import BasicAuthMiddleware
from .config import settings
from .database import engine, get_session, init_db
from .models import Dictionary, Flashcard, Job, MinedCard, Phrase, StudySession, Video
from .languages import normalize_lang
from .schemas import DailyComplete, FlashcardCreate, PendingAnkiCreate, PendingAnkiSynced, PhraseCreate, PhraseUpdate, VideoURLCreate, VOTSubtitleRequest
from .services.anki import build_ankiconnect_note, build_selected_deck
from .services.dictionary_upload import DictionaryUploadError, install_uploaded_dictionary
from .services.google_sheets import GoogleSheetsError, append_weak_spots, auth_status, build_login_url, complete_daily_task, exchange_code, get_daily_task
from .services.media import extract_audio
from .services.stardict import StarDictDictionary, StarDictError, get_stardict
from .services.vot import vot_status


def now():
    return datetime.now(timezone.utc)


app = FastAPI(title="LexiQuest Cake", version="0.11.0")
app.add_middleware(BasicAuthMiddleware)


@app.on_event("startup")
def startup():
    init_db()


def new_video_id() -> str:
    return uuid.uuid4().hex[:12]


def serialize_video(video: Video, session: Session) -> dict:
    job = session.exec(select(Job).where(Job.video_id == video.id).order_by(Job.id.desc())).first()
    return {
        **video.model_dump(),
        "stream_url": f"/api/videos/{video.id}/stream" if video.status == "ready" else None,
        "job": job.model_dump() if job else None,
    }


@app.get("/api/health")
def health():
    return {"ok": True, "service": "lexiquest-cake", "version": "0.11.0"}





@app.get("/api/auth/google/login")
def google_login():
    try:
        return RedirectResponse(build_login_url(), status_code=302)
    except GoogleSheetsError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/auth/google/callback")
def google_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        raise HTTPException(400, f"Google OAuth: {error}")
    if not code:
        raise HTTPException(400, "Missing OAuth code")
    try:
        exchange_code(code, state)
    except GoogleSheetsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/?google=connected", status_code=302)


@app.get("/api/auth/google/status")
def google_status():
    return auth_status()


@app.get("/api/daily/task")
def daily_task(session: Session = Depends(get_session)):
    try:
        task = get_daily_task()
    except GoogleSheetsError as exc:
        raise HTTPException(503, str(exc)) from exc

    video = session.exec(select(Video).where(Video.source_url == task["video_url"]).order_by(Video.created_at.desc())).first()
    if not video:
        video = Video(
            id=new_video_id(),
            source_url=task["video_url"],
            source_provider="url",
            title=task.get("grammar_topic") or "Daily lesson",
            source_lang="en",
            target_lang="ru",
            status="queued",
        )
        session.add(video)
        session.commit()
        session.refresh(video)
        session.add(
            Job(
                video_id=video.id,
                payload_json=json.dumps({"kind": "url", "url": task["video_url"], "title": None}),
            )
        )
        session.commit()
    task["video"] = serialize_video(video, session)
    task["video_id"] = video.id
    return task


@app.get("/api/anki/state")
def anki_state(video_id: str | None = None, session: Session = Depends(get_session)):
    statement = select(MinedCard)
    if video_id:
        statement = statement.where(MinedCard.video_id == video_id)
    cards = session.exec(statement.order_by(MinedCard.created_at)).all()
    return [
        {
            "id": card.id,
            "video_id": card.video_id,
            "phrase_id": card.phrase_id,
            "source_word": card.source_word,
            "status": card.status,
            "anki_note_id": card.anki_note_id,
            "synced_at": card.synced_at,
        }
        for card in cards
    ]


@app.get("/api/anki/pending")
def pending_anki(session: Session = Depends(get_session)):
    cards = session.exec(
        select(MinedCard).where(MinedCard.status == "pending_anki").order_by(MinedCard.created_at)
    ).all()
    return [
        {
            **card.model_dump(),
            "anki_note": build_ankiconnect_note(card, f"lq_pending_{card.id}.mp3" if card.clip_start is not None and card.clip_end is not None else None),
            "audio_url": f"/api/anki/pending/{card.id}/audio" if card.clip_start is not None and card.clip_end is not None and card.video_id else None,
        }
        for card in cards
    ]


@app.post("/api/anki/pending")
def create_pending_anki(body: PendingAnkiCreate, session: Session = Depends(get_session)):
    existing = session.exec(
        select(MinedCard).where(
            MinedCard.video_id == body.video_id,
            MinedCard.phrase_id == body.phrase_id,
            MinedCard.source_word == body.source_word,
        ).order_by(MinedCard.id.desc())
    ).first()
    card = existing or MinedCard(**body.model_dump(), status="pending_anki")
    if not existing:
        session.add(card)
        session.commit()
        session.refresh(card)
    return {
        **card.model_dump(),
        "anki_note": build_ankiconnect_note(card, f"lq_pending_{card.id}.mp3" if card.clip_start is not None and card.clip_end is not None else None),
    }


@app.get("/api/anki/pending/{card_id}/audio")
def pending_anki_audio(card_id: int, session: Session = Depends(get_session)):
    card = session.get(MinedCard, card_id)
    if not card or not card.video_id or card.clip_start is None or card.clip_end is None:
        raise HTTPException(404, "Audio clip is unavailable")
    video = session.get(Video, card.video_id)
    if not video or not video.file_path:
        raise HTTPException(404, "Video is unavailable")
    output = settings.media_root / "audio" / f"lq_pending_{card.id}.mp3"
    if not output.exists():
        extract_audio(Path(video.file_path), float(card.clip_start), float(card.clip_end), output)
    return FileResponse(output, media_type="audio/mpeg", filename=output.name)


@app.post("/api/anki/pending/{card_id}/synced")
def mark_pending_anki_synced(card_id: int, body: PendingAnkiSynced, session: Session = Depends(get_session)):
    card = session.get(MinedCard, card_id)
    if not card:
        raise HTTPException(404, "Pending card not found")
    card.status = "synced"
    card.anki_note_id = body.note_id
    card.synced_at = now()
    session.add(card)
    session.commit()
    return {"ok": True}


@app.post("/api/daily/complete")
def complete_daily(body: DailyComplete, session: Session = Depends(get_session)):
    lookup_events = [event.model_dump() for event in body.lookup_events]
    study = StudySession(
        study_date=body.date,
        video_id=body.video_id,
        time_spent_seconds=body.time_spent_seconds,
        completed_highlights=body.completed_highlights,
        looked_up_words_json=json.dumps(body.looked_up_words, ensure_ascii=False),
        lookup_events_json=json.dumps(lookup_events, ensure_ascii=False),
        mined_cards_count=body.mined_cards_count,
    )
    session.add(study)
    session.commit()
    session.refresh(study)

    sheets_result = None
    weak_spots = 0
    try:
        sheets_result = complete_daily_task(body.date, body.mined_cards_count, body.time_spent_seconds)
        weak_spots = append_weak_spots(body.date, lookup_events)
    except GoogleSheetsError as exc:
        return {
            "ok": True,
            "session_id": study.id,
            "google_updated": False,
            "google_error": str(exc),
            "weak_spots_appended": 0,
        }
    return {
        "ok": True,
        "session_id": study.id,
        "google_updated": True,
        "progress": sheets_result,
        "weak_spots_appended": weak_spots,
    }


@app.get("/api/vot/status")
def get_vot_status():
    return vot_status(settings.vot_bridge_url)


@app.post("/api/videos/{video_id}/vot-subtitles")
def queue_vot_subtitles(
    video_id: str,
    body: VOTSubtitleRequest | None = None,
    session: Session = Depends(get_session),
):
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Video not found")
    if video.status != "ready":
        raise HTTPException(409, "Video must be ready first")
    if not video.source_url:
        raise HTTPException(400, "VOT needs the original online video URL; uploaded local files need another subtitle/ASR provider")

    source_lang = normalize_lang(body.source_lang if body else video.source_lang, "auto")[:16]
    target_lang = normalize_lang(body.target_lang if body else video.target_lang, "ru")[:16]
    if source_lang == target_lang and source_lang != "auto":
        raise HTTPException(400, "Source and target languages must be different")

    existing = session.exec(
        select(Job).where(Job.video_id == video_id, Job.kind == "vot_subtitles", Job.status.in_(["queued", "running"]))
    ).first()
    if existing:
        return existing

    job = Job(
        video_id=video_id,
        kind="vot_subtitles",
        status="queued",
        progress=0,
        message=f"VOT subtitles queued: {source_lang} → {target_lang}",
        payload_json=json.dumps({
            "kind": "vot_subtitles",
            "source_lang": source_lang,
            "target_lang": target_lang,
            "regenerate": True,
        }),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@app.get("/api/videos")
def list_videos(session: Session = Depends(get_session)):
    videos = session.exec(select(Video).order_by(Video.created_at.desc())).all()
    return [serialize_video(v, session) for v in videos]


@app.post("/api/videos/url")
def add_video_url(body: VideoURLCreate, session: Session = Depends(get_session)):
    video_id = new_video_id()
    video = Video(
        id=video_id,
        source_url=body.url,
        source_provider="url",
        title=body.title or "New video",
        source_lang=body.source_lang,
        target_lang=body.target_lang,
        status="queued",
    )
    session.add(video)
    session.commit()
    job = Job(
        video_id=video_id,
        payload_json=json.dumps(
            {
                "kind": "url",
                "url": body.url,
                "title": body.title,
            }
        ),
    )
    session.add(job)
    session.commit()
    session.refresh(video)
    return serialize_video(video, session)


@app.post("/api/videos/upload")
async def upload_video(
    file: UploadFile = File(...),
    title: str = Form("Uploaded video"),
    source_lang: str = Form("en"),
    target_lang: str = Form("ru"),
    session: Session = Depends(get_session),
):
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}:
        raise HTTPException(400, "Unsupported video extension")

    video_id = new_video_id()
    upload_dir = settings.media_root / "tmp" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    upload_path = upload_dir / f"{video_id}{suffix}"
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0

    with upload_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                out.close()
                upload_path.unlink(missing_ok=True)
                raise HTTPException(413, f"Upload exceeds {settings.max_upload_mb} MB")
            out.write(chunk)

    video = Video(
        id=video_id,
        source_provider="upload",
        title=title.strip()[:300] or "Uploaded video",
        source_lang=normalize_lang(source_lang, "en")[:16],
        target_lang=normalize_lang(target_lang, "ru")[:16],
        status="queued",
    )
    session.add(video)
    session.commit()
    session.add(
        Job(
            video_id=video_id,
            payload_json=json.dumps({"kind": "upload", "upload_path": str(upload_path)}),
        )
    )
    session.commit()
    return serialize_video(video, session)


@app.get("/api/videos/{video_id}")
def get_video(video_id: str, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Video not found")
    return serialize_video(video, session)


def _safe_unlink(path: Path, root: Path, cleanup_errors: list[str]) -> None:
    try:
        resolved = path.resolve()
        allowed = root.resolve()
        if resolved != allowed and allowed not in resolved.parents:
            cleanup_errors.append(f"Skipped path outside storage root: {resolved}")
            return
        resolved.unlink(missing_ok=True)
    except Exception as exc:
        cleanup_errors.append(f"{path}: {exc}")


def _safe_rmtree(path: Path, root: Path, cleanup_errors: list[str]) -> None:
    try:
        resolved = path.resolve()
        allowed = root.resolve()
        if resolved == allowed or allowed not in resolved.parents:
            cleanup_errors.append(f"Skipped directory outside storage root: {resolved}")
            return
        shutil.rmtree(resolved, ignore_errors=False)
    except FileNotFoundError:
        pass
    except Exception as exc:
        cleanup_errors.append(f"{path}: {exc}")


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Video not found")

    # Never delete a lesson while the worker may still be writing its media or
    # rebuilding its VOT timeline. This keeps deletion deterministic and avoids
    # orphan files / stale SQLAlchemy sessions.
    active_job = session.exec(
        select(Job).where(
            Job.video_id == video_id,
            Job.status.in_(["queued", "running"]),
        )
    ).first()
    if active_job:
        raise HTTPException(
            409,
            "Lesson is still processing. Wait until the current job finishes, then delete it.",
        )

    phrases = session.exec(select(Phrase).where(Phrase.video_id == video_id)).all()
    phrase_ids = [p.id for p in phrases if p.id is not None]
    cards = (
        session.exec(select(Flashcard).where(Flashcard.phrase_id.in_(phrase_ids))).all()
        if phrase_ids else []
    )
    jobs = session.exec(select(Job).where(Job.video_id == video_id)).all()

    counts = {
        "phrases": len(phrases),
        "flashcards": len(cards),
        "jobs": len(jobs),
    }
    video_file_path = video.file_path

    # Delete relational data first. Media cleanup happens immediately after a
    # successful commit and is restricted to LexiQuest-owned directories.
    for item in cards:
        session.delete(item)
    for item in phrases:
        session.delete(item)
    for item in jobs:
        session.delete(item)
    session.delete(video)
    session.commit()

    cleanup_errors: list[str] = []

    if video_file_path:
        _safe_unlink(Path(video_file_path), settings.media_root / "videos", cleanup_errors)

    audio_dir = settings.media_root / "audio"
    for clip in audio_dir.glob(f"lq_{video_id}_*.mp3"):
        _safe_unlink(clip, audio_dir, cleanup_errors)

    # Remove any unfinished ingestion/VOT work directories and an uploaded
    # source file that may have survived an interrupted worker.
    tmp_root = settings.media_root / "tmp"
    _safe_rmtree(tmp_root / video_id, tmp_root, cleanup_errors)
    _safe_rmtree(tmp_root / f"vot-{video_id}", tmp_root, cleanup_errors)
    uploads_dir = tmp_root / "uploads"
    for uploaded in uploads_dir.glob(f"{video_id}.*"):
        _safe_unlink(uploaded, uploads_dir, cleanup_errors)

    export_path = settings.export_dir / f"lexiquest_{video_id}.apkg"
    _safe_unlink(export_path, settings.export_dir, cleanup_errors)

    return {
        "ok": True,
        "video_id": video_id,
        "deleted": counts,
        "cleanup_errors": cleanup_errors,
    }


@app.get("/api/videos/{video_id}/stream")
def stream_video(video_id: str, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video or video.status != "ready" or not video.file_path:
        raise HTTPException(404, "Video not ready")
    path = Path(video.file_path).resolve()
    root = (settings.media_root / "videos").resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "Media file not found")
    return FileResponse(path, media_type="video/mp4", content_disposition_type="inline")


@app.get("/api/videos/{video_id}/phrases")
def list_phrases(video_id: str, session: Session = Depends(get_session)):
    if not session.get(Video, video_id):
        raise HTTPException(404, "Video not found")
    return session.exec(
        select(Phrase).where(Phrase.video_id == video_id).order_by(Phrase.order_index, Phrase.start_time)
    ).all()


@app.post("/api/videos/{video_id}/phrases")
def create_phrase(video_id: str, body: PhraseCreate, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Video not found")
    if body.end_time <= body.start_time:
        raise HTTPException(400, "end_time must be greater than start_time")
    if body.order_index is None:
        existing = session.exec(select(Phrase).where(Phrase.video_id == video_id)).all()
        order_index = len(existing)
    else:
        order_index = body.order_index
    phrase = Phrase(video_id=video_id, order_index=order_index, provider="manual", **body.model_dump(exclude={"order_index"}))
    session.add(phrase)
    session.commit()
    session.refresh(phrase)
    return phrase


@app.patch("/api/phrases/{phrase_id}")
def update_phrase(phrase_id: int, body: PhraseUpdate, session: Session = Depends(get_session)):
    phrase = session.get(Phrase, phrase_id)
    if not phrase:
        raise HTTPException(404, "Phrase not found")
    updates = body.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(phrase, key, value)
    phrase.provider = "manual"
    if phrase.end_time <= phrase.start_time:
        raise HTTPException(400, "end_time must be greater than start_time")
    phrase.updated_at = now()
    session.add(phrase)
    session.commit()
    session.refresh(phrase)
    return phrase


@app.delete("/api/phrases/{phrase_id}")
def delete_phrase(phrase_id: int, session: Session = Depends(get_session)):
    phrase = session.get(Phrase, phrase_id)
    if not phrase:
        raise HTTPException(404, "Phrase not found")
    cards = session.exec(select(Flashcard).where(Flashcard.phrase_id == phrase_id)).all()
    for card in cards:
        session.delete(card)
    session.delete(phrase)
    session.commit()
    return {"ok": True}


def _video_phrase_map(video_id: str, session: Session) -> dict[int, Phrase]:
    phrases = session.exec(select(Phrase).where(Phrase.video_id == video_id)).all()
    return {p.id: p for p in phrases if p.id is not None}


@app.get("/api/videos/{video_id}/flashcards")
def list_flashcards(video_id: str, session: Session = Depends(get_session)):
    if not session.get(Video, video_id):
        raise HTTPException(404, "Video not found")
    phrases = _video_phrase_map(video_id, session)
    if not phrases:
        return []
    cards = session.exec(select(Flashcard).where(Flashcard.phrase_id.in_(list(phrases.keys()))).order_by(Flashcard.created_at)).all()
    return [
        {
            "id": c.id,
            "phrase_id": c.phrase_id,
            "source_word": c.source_word,
            "target_word": c.target_word,
            "dictionary_name": c.dictionary_name,
            "pronunciation": c.pronunciation,
            "source_phrase": c.source_phrase_snapshot or phrases[c.phrase_id].source_text,
            "target_phrase": c.target_phrase_snapshot if c.target_phrase_snapshot is not None else phrases[c.phrase_id].translated_text,
            "clip_start": c.clip_start if c.clip_start is not None else phrases[c.phrase_id].start_time,
            "clip_end": c.clip_end if c.clip_end is not None else phrases[c.phrase_id].end_time,
            "created_at": c.created_at,
        }
        for c in cards if c.phrase_id in phrases
    ]


@app.post("/api/phrases/{phrase_id}/flashcards")
def save_flashcard(phrase_id: int, body: FlashcardCreate, session: Session = Depends(get_session)):
    phrase = session.get(Phrase, phrase_id)
    if not phrase:
        raise HTTPException(404, "Phrase not found")
    source = body.source_word.strip()
    target = body.target_word.strip()
    existing = session.exec(select(Flashcard).where(Flashcard.phrase_id == phrase_id)).all()
    for card in existing:
        if (card.source_word or "").strip().casefold() == source.casefold():
            card.source_word = source
            card.target_word = target
            card.dictionary_name = body.dictionary_name
            card.pronunciation = body.pronunciation
            card.source_phrase_snapshot = phrase.source_text
            card.target_phrase_snapshot = phrase.translated_text or ""
            card.clip_start = phrase.start_time
            card.clip_end = phrase.end_time
            session.add(card)
            session.commit()
            session.refresh(card)
            return card
    card = Flashcard(
        phrase_id=phrase_id,
        source_word=source,
        target_word=target,
        dictionary_name=body.dictionary_name,
        pronunciation=body.pronunciation,
        source_phrase_snapshot=phrase.source_text,
        target_phrase_snapshot=phrase.translated_text or "",
        clip_start=phrase.start_time,
        clip_end=phrase.end_time,
    )
    session.add(card)
    session.commit()
    session.refresh(card)
    return card


@app.delete("/api/flashcards/{card_id}")
def delete_flashcard(card_id: int, session: Session = Depends(get_session)):
    card = session.get(Flashcard, card_id)
    if not card:
        raise HTTPException(404, "Flashcard not found")
    session.delete(card)
    session.commit()
    return {"ok": True}


@app.get("/api/videos/{video_id}/lesson-summary")
def lesson_summary(video_id: str, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Video not found")
    phrases = _video_phrase_map(video_id, session)
    cards = []
    if phrases:
        cards = session.exec(select(Flashcard).where(Flashcard.phrase_id.in_(list(phrases.keys())))).all()
    return {
        "video_id": video_id,
        "title": video.title,
        "duration": video.duration or 0,
        "phrase_count": len(phrases),
        "saved_word_count": len(cards),
    }


@app.get("/api/videos/{video_id}/anki")
def export_anki(video_id: str, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video or video.status != "ready" or not video.file_path:
        raise HTTPException(404, "Video not ready")
    phrases = _video_phrase_map(video_id, session)
    if not phrases:
        raise HTTPException(400, "No phrases available")
    cards = session.exec(select(Flashcard).where(Flashcard.phrase_id.in_(list(phrases.keys()))).order_by(Flashcard.created_at)).all()
    cards = [c for c in cards if c.source_word and c.target_word]
    if not cards:
        raise HTTPException(400, "No saved words to export")
    output = settings.export_dir / f"lexiquest_{video_id}.apkg"
    build_selected_deck(video, cards, phrases, Path(video.file_path), settings.media_root / "audio", output)
    return FileResponse(output, media_type="application/octet-stream", filename=output.name)


@app.get("/api/dictionaries")
def list_dictionaries(session: Session = Depends(get_session)):
    return session.exec(select(Dictionary).order_by(Dictionary.name)).all()


@app.post("/api/dictionaries/scan")
def scan_dictionaries(
    source_lang: str = "en",
    target_lang: str = "ru",
    session: Session = Depends(get_session),
):
    source_lang = normalize_lang(source_lang, "en")[:16]
    target_lang = normalize_lang(target_lang, "ru")[:16]
    added = []
    for ifo in settings.dictionary_dir.rglob("*.ifo"):
        if ".upload-tmp" in ifo.parts:
            continue
        base = str(ifo)[:-4]
        existing = session.exec(select(Dictionary).where(Dictionary.base_path == base)).first()
        if existing:
            continue
        try:
            sd = StarDictDictionary(Path(base))
            name = sd.meta.get("bookname", ifo.stem)
            item = Dictionary(
                name=name,
                source_lang=source_lang,
                target_lang=target_lang,
                base_path=base,
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            added.append(item)
        except StarDictError:
            continue
    return {"added": added}


@app.post("/api/dictionaries/upload")
async def upload_dictionary(
    files: list[UploadFile] = File(...),
    source_lang: str = Form("en"),
    target_lang: str = Form("ru"),
    session: Session = Depends(get_session),
):
    source_lang = normalize_lang(source_lang, "en")[:16]
    target_lang = normalize_lang(target_lang, "ru")[:16]
    if not files:
        raise HTTPException(400, "Choose a StarDict ZIP or dictionary files")

    upload_root = settings.dictionary_dir / ".upload-tmp" / uuid.uuid4().hex
    upload_root.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    max_bytes = settings.max_dictionary_upload_mb * 1024 * 1024
    total = 0
    try:
        for file in files:
            name = Path(file.filename or "dictionary.bin").name
            if not name.lower().endswith((".zip", ".ifo", ".idx", ".dict", ".dict.dz", ".syn")):
                raise HTTPException(400, f"Unsupported dictionary file: {name}")
            path = upload_root / name
            with path.open("wb") as out:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(413, f"Dictionary upload exceeds {settings.max_dictionary_upload_mb} MB")
                    out.write(chunk)
            saved.append(path)

        try:
            installed = install_uploaded_dictionary(saved, settings.dictionary_dir)
        except (DictionaryUploadError, StarDictError) as exc:
            raise HTTPException(400, str(exc)) from exc

        added = []
        for base, book_name in installed:
            existing = session.exec(select(Dictionary).where(Dictionary.base_path == str(base))).first()
            if existing:
                added.append(existing)
                continue
            item = Dictionary(
                name=book_name[:300],
                format="stardict",
                source_lang=source_lang,
                target_lang=target_lang,
                base_path=str(base),
                enabled=True,
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            added.append(item)
        return {"added": added}
    finally:
        shutil.rmtree(upload_root, ignore_errors=True)


@app.delete("/api/dictionaries/{dictionary_id}")
def delete_dictionary(dictionary_id: int, session: Session = Depends(get_session)):
    item = session.get(Dictionary, dictionary_id)
    if not item:
        raise HTTPException(404, "Dictionary not found")
    base = Path(item.base_path)
    root = settings.dictionary_dir.resolve()
    try:
        parent = base.parent.resolve()
        if root in parent.parents and parent != root:
            shutil.rmtree(parent, ignore_errors=True)
        else:
            for ext in (".ifo", ".idx", ".dict", ".dict.dz", ".syn"):
                Path(str(base) + ext).unlink(missing_ok=True)
    except Exception:
        pass
    session.delete(item)
    session.commit()
    return {"ok": True}


@app.get("/api/dictionaries/lookup")
def lookup_dictionary(
    word: str,
    source_lang: str,
    target_lang: str,
    session: Session = Depends(get_session),
):
    source_lang = normalize_lang(source_lang, "en")[:16]
    target_lang = normalize_lang(target_lang, "ru")[:16]
    dictionaries = session.exec(
        select(Dictionary).where(
            Dictionary.enabled == True,  # noqa: E712
            Dictionary.source_lang == source_lang,
            Dictionary.target_lang == target_lang,
        )
    ).all()
    results = []
    for item in dictionaries:
        try:
            definition = get_stardict(item.base_path).lookup(word)
            if definition:
                results.append({"dictionary": item.name, "definition": definition})
        except Exception:
            continue
    return {"word": word, "results": results}


STATIC = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


@app.get("/")
def root():
    return FileResponse(STATIC / "index.html")
