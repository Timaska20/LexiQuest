from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from .config import settings
from .database import engine, init_db
from .languages import normalize_lang
from .models import Flashcard, Job, Phrase, Video
from .services.media import copy_uploaded, download_url, probe, transcode
from .services.vot import VOTError, align_translations, build_synced_phrase_pairs, fetch_vot_subtitle_bundle


def now():
    return datetime.now(timezone.utc)


def update_job(session: Session, job: Job, progress: int, message: str):
    job.progress = progress
    job.message = message
    job.updated_at = now()
    session.add(job)
    session.commit()
    session.refresh(job)


def claim_job(session: Session) -> Job | None:
    job = session.exec(select(Job).where(Job.status == "queued").order_by(Job.id)).first()
    if not job:
        return None
    job.status = "running"
    job.progress = 1
    job.message = "Starting"
    job.updated_at = now()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job



def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _best_phrase_for_card(old_phrase: Phrase, card: Flashcard, new_phrases: list[Phrase]) -> Phrase:
    old_start = float(old_phrase.start_time)
    old_end = float(old_phrase.end_time)
    old_mid = (old_start + old_end) / 2
    old_duration = max(0.05, old_end - old_start)
    word = (card.source_word or "").strip().casefold()

    def score(p: Phrase) -> float:
        p_start = float(p.start_time)
        p_end = float(p.end_time)
        p_mid = (p_start + p_end) / 2
        overlap = _overlap(old_start, old_end, p_start, p_end) / old_duration
        contains_word = bool(word and word in (p.source_text or "").casefold())
        return overlap * 10.0 + (6.0 if contains_word else 0.0) - abs(p_mid - old_mid) * 0.08

    return max(new_phrases, key=score)


def _replace_with_vot_timeline(session: Session, video: Video, pairs: list[dict], *, preserve_cards: bool = True) -> int:
    """Atomically replace mixed YouTube/VOT phrases with one VOT timeline.

    Existing saved words are remapped by their old time position and immediately
    snapshotted to the new synchronized source/target phrase + audio boundaries.
    """
    old_phrases = session.exec(
        select(Phrase).where(Phrase.video_id == video.id).order_by(Phrase.order_index, Phrase.start_time)
    ).all()
    old_by_id = {p.id: p for p in old_phrases if p.id is not None}
    old_ids = list(old_by_id)
    cards = session.exec(select(Flashcard).where(Flashcard.phrase_id.in_(old_ids))).all() if old_ids else []

    new_phrases: list[Phrase] = []
    for i, pair in enumerate(pairs):
        row = Phrase(
            video_id=video.id,
            start_time=float(pair["start"]),
            end_time=float(pair["end"]),
            source_text=str(pair["source_text"]).strip(),
            translated_text=str(pair.get("translated_text") or "").strip(),
            provider="vot",
            order_index=i,
        )
        session.add(row)
        new_phrases.append(row)

    session.flush()  # allocate IDs before remapping flashcards

    if not new_phrases:
        raise VOTError("VOT synchronization produced no usable phrases")

    if preserve_cards:
        for card in cards:
            old_phrase = old_by_id.get(card.phrase_id)
            if not old_phrase:
                continue
            new_phrase = _best_phrase_for_card(old_phrase, card, new_phrases)
            card.phrase_id = int(new_phrase.id)
            card.source_phrase_snapshot = new_phrase.source_text
            card.target_phrase_snapshot = new_phrase.translated_text or ""
            card.clip_start = float(new_phrase.start_time)
            card.clip_end = float(new_phrase.end_time)
            session.add(card)
    else:
        # A saved dictionary word belongs to a language pair. If either side of
        # the lesson changes, keeping the old word/meaning would create an Anki
        # card in the wrong language, so remove those selections explicitly.
        for card in cards:
            session.delete(card)

    session.flush()
    for phrase in old_phrases:
        session.delete(phrase)
    session.flush()
    return len(new_phrases)


def process_vot_subtitles(job_id: int):
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            return
        video = session.get(Video, job.video_id)
        if not video or not video.source_url:
            job.status = "failed"
            job.message = "Original source URL is required for VOT"
            session.add(job)
            session.commit()
            return

        payload = json.loads(job.payload_json or "{}")
        requested_source = normalize_lang(payload.get("source_lang") or video.source_lang, "auto")[:16]
        requested_target = normalize_lang(payload.get("target_lang") or video.target_lang, "ru")[:16]
        workdir = settings.media_root / "tmp" / f"vot-{video.id}"

        try:
            phrases = session.exec(
                select(Phrase).where(Phrase.video_id == video.id).order_by(Phrase.order_index, Phrase.start_time)
            ).all()

            update_job(
                session,
                job,
                10,
                f"VOT: regenerating subtitles {requested_source} → {requested_target}",
            )
            bundle = fetch_vot_subtitle_bundle(
                settings.vot_bridge_url,
                video.source_url,
                requested_source,
                requested_target,
                workdir,
            )
            source_cues = bundle["source_cues"]
            target_cues = bundle["target_cues"]
            selected_meta = bundle.get("selected") or {}
            detected_source = normalize_lang(
                selected_meta.get("translatedFromLanguage")
                or selected_meta.get("sourceLanguage")
                or selected_meta.get("language")
                or bundle.get("source_language")
                or (None if requested_source == "auto" else requested_source),
                video.source_lang,
            )[:16]
            effective_source = detected_source if requested_source == "auto" else requested_source
            if not effective_source or effective_source == "auto":
                effective_source = video.source_lang
            language_pair_changed = (
                normalize_lang(video.source_lang) != normalize_lang(effective_source)
                or normalize_lang(video.target_lang) != normalize_lang(requested_target)
            )

            if source_cues:
                update_job(
                    session,
                    job,
                    75,
                    f"VOT: synchronizing {len(source_cues)} source / {len(target_cues)} translated cues",
                )
                pairs = build_synced_phrase_pairs(source_cues, target_cues)
                synced = _replace_with_vot_timeline(session, video, pairs, preserve_cards=not language_pair_changed)
                mode = "paired VOT timeline"
            else:
                # Compatibility with an older VOT bridge during rolling upgrades.
                if not phrases:
                    raise VOTError("No source phrases and VOT bridge returned no source track")
                update_job(session, job, 80, f"VOT: legacy-aligning {len(target_cues)} translated cues")
                synced = align_translations(phrases, target_cues)
                for phrase in phrases:
                    session.add(phrase)
                mode = "legacy target-only alignment"

            # Only change lesson language metadata after a successful regeneration.
            # With AUTO, store the language actually selected/detected by VOT so
            # dictionary lookup and Intl.Segmenter use the real source language.
            video.source_lang = effective_source
            video.target_lang = requested_target
            video.updated_at = now()
            session.add(video)

            job.status = "done"
            job.progress = 100
            cards_note = "; saved words cleared because language pair changed" if language_pair_changed else ""
            job.message = (
                f"VOT subtitles ready: {synced} phrases; "
                f"{video.source_lang} → {video.target_lang} ({mode}){cards_note}"
            )
            job.updated_at = now()
            session.add(job)
            session.commit()
            print(
                f"[VOT {video.id}] regenerated={synced} source={len(source_cues)} target={len(target_cues)} "
                f"mode={mode} requested={requested_source}->{requested_target} "
                f"detected={detected_source or '?'} stored={video.source_lang}->{video.target_lang}",
                flush=True,
            )
        except Exception as exc:
            session.rollback()
            job = session.get(Job, job_id)
            if job:
                job.status = "failed"
                job.message = str(exc)[-800:]
                job.updated_at = now()
                session.add(job)
                session.commit()
            print(f"[VOT {video.id}] failed: {exc}", flush=True)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

def process_job(job_id: int):
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            return
        video = session.get(Video, job.video_id)
        if not video:
            job.status = "failed"
            job.message = "Video row missing"
            session.add(job)
            session.commit()
            return

        payload = json.loads(job.payload_json or "{}")
        temp_dir = settings.media_root / "tmp" / video.id
        final_path = settings.media_root / "videos" / f"{video.id}.mp4"
        phrases: list[dict] = []

        try:
            video.status = "processing"
            video.error_message = None
            video.updated_at = now()
            session.add(video)
            session.commit()

            if payload.get("kind") == "url":
                update_job(session, job, 10, "Downloading video")
                input_path, discovered_title, phrases = download_url(
                    payload["url"], temp_dir, video.source_lang
                )
                if not payload.get("title"):
                    video.title = discovered_title[:300] or video.title
            else:
                update_job(session, job, 10, "Preparing upload")
                input_path = copy_uploaded(Path(payload["upload_path"]), temp_dir)

            update_job(session, job, 45, "Transcoding to H.264/AAC")
            transcode(input_path, final_path)

            update_job(session, job, 80, "Checking media")
            meta = probe(final_path)
            video.file_path = str(final_path)
            video.file_size_bytes = final_path.stat().st_size
            video.duration = meta["duration"]
            video.width = meta["width"]
            video.height = meta["height"]
            video.video_codec = meta["video_codec"]
            video.audio_codec = meta["audio_codec"]

            existing = session.exec(select(Phrase).where(Phrase.video_id == video.id)).first()
            if phrases and not existing:
                update_job(session, job, 90, f"Importing {len(phrases)} subtitle cues")
                for i, cue in enumerate(phrases):
                    session.add(
                        Phrase(
                            video_id=video.id,
                            start_time=cue["start"],
                            end_time=cue["end"],
                            source_text=cue["text"],
                            translated_text="",
                            order_index=i,
                        )
                    )

            video.status = "ready"
            video.updated_at = now()
            session.add(video)
            job.status = "done"
            job.progress = 100
            job.message = "Ready"
            job.updated_at = now()
            session.add(job)

            # v0.9 Kazakh mode: URL lessons marked kk are VOT-first. YouTube
            # often has no Kazakh caption track, so queue Yandex/VOT speech
            # transcription + RU translation automatically after ingest.
            if video.source_url and video.source_lang.split("-")[0] == "kk":
                existing_vot = session.exec(
                    select(Job).where(
                        Job.video_id == video.id,
                        Job.kind == "vot_subtitles",
                        Job.status.in_(["queued", "running"]),
                    )
                ).first()
                if not existing_vot:
                    session.add(
                        Job(
                            video_id=video.id,
                            kind="vot_subtitles",
                            status="queued",
                            progress=0,
                            message="Kazakh mode: VOT transcription queued",
                            payload_json=json.dumps({"kind": "vot_subtitles", "auto": True}),
                        )
                    )
                    print(f"[VOT {video.id}] Kazakh auto-VOT queued", flush=True)

            session.commit()

        except Exception as exc:
            video.status = "failed"
            video.error_message = str(exc)[-1500:]
            video.updated_at = now()
            job.status = "failed"
            job.message = str(exc)[-800:]
            job.updated_at = now()
            session.add(video)
            session.add(job)
            session.commit()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            upload_path = payload.get("upload_path")
            if upload_path:
                try:
                    Path(upload_path).unlink(missing_ok=True)
                except Exception:
                    pass


def main():
    init_db()
    # A single MVP worker is used. If the process was killed mid-job, make the
    # persistent job retryable after restart instead of leaving it stuck forever.
    with Session(engine) as session:
        stuck = session.exec(select(Job).where(Job.status == "running")).all()
        for job in stuck:
            job.status = "queued"
            job.message = "Recovered after worker restart"
            job.updated_at = now()
            session.add(job)
        if stuck:
            session.commit()
    print("LexiQuest worker started", flush=True)
    while True:
        try:
            with Session(engine) as session:
                job = claim_job(session)
                job_id = job.id if job else None
            if job_id:
                with Session(engine) as session:
                    claimed = session.get(Job, job_id)
                    kind = claimed.kind if claimed else "ingest"
                    video_id = claimed.video_id if claimed else "?"
                print(f"[JOB {job_id}] {kind} video={video_id} started", flush=True)
                if kind == "vot_subtitles":
                    process_vot_subtitles(job_id)
                else:
                    process_job(job_id)
                print(f"[JOB {job_id}] {kind} video={video_id} finished", flush=True)
            else:
                time.sleep(1.0)
        except Exception as exc:
            print(f"Worker loop error: {exc}", flush=True)
            time.sleep(2.0)


if __name__ == "__main__":
    main()
