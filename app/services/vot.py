from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from .vtt import parse_vtt


class VOTError(RuntimeError):
    pass


def _request_json(url: str, *, method: str = "GET", payload: dict | None = None, timeout: int = 15) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        try:
            parsed = json.loads(detail)
            detail = parsed.get("error") or parsed.get("detail") or parsed.get("message") or detail
        except Exception:
            pass
        raise VOTError(f"VOT bridge HTTP {exc.code}: {detail[-1200:]}") from exc
    except Exception as exc:
        raise VOTError(f"VOT bridge unavailable: {exc}") from exc

    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise VOTError("VOT bridge returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise VOTError("VOT bridge returned an unexpected response")
    return value


def vot_status(bridge_url: str) -> dict:
    try:
        payload = _request_json(f"{bridge_url.rstrip('/')}/health", timeout=5)
        return {
            "available": bool(payload.get("ok")),
            "bridge": payload.get("service", "lexiquest-vot-bridge"),
            "vot_js_version": payload.get("votJsVersion"),
            "node": payload.get("node"),
        }
    except Exception as exc:
        return {
            "available": False,
            "bridge": "lexiquest-vot-bridge",
            "vot_js_version": None,
            "node": None,
            "error": str(exc),
        }


def _normalize_bridge_cues(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    normalized: list[dict] = []
    seen = set()
    for cue in value:
        if not isinstance(cue, dict):
            continue
        try:
            start = float(cue["start"])
            end = float(cue["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = re.sub(r"\s+", " ", str(cue.get("text") or "")).strip()
        if not text or not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        key = (round(start, 3), round(end, 3), text)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"start": start, "end": end, "text": text})
    normalized.sort(key=lambda cue: (cue["start"], cue["end"]))
    return normalized


def _vtt_fallback(payload: dict, workdir: Path) -> list[dict]:
    vtt = payload.get("vtt")
    if not isinstance(vtt, str) or not vtt.strip():
        return []
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "translated.vtt"
    path.write_text(vtt, encoding="utf-8")
    return parse_vtt(path)


def fetch_vot_subtitle_bundle(
    bridge_url: str,
    url: str,
    source_lang: str,
    target_lang: str,
    workdir: Path,
    timeout_seconds: int = 420,
) -> dict:
    """Return VOT source + translated cues from the *same* subtitle track.

    v0.6 mixed YouTube source timings with Yandex/VOT translation timings. Those
    are different segmentation systems and inevitably drift. v0.7 asks the Node
    bridge for both sides of the selected VOT track so source text, translation,
    and local-video playback all share one timeline.
    """
    payload = _request_json(
        f"{bridge_url.rstrip('/')}/subtitles",
        method="POST",
        payload={
            "url": url,
            "sourceLang": source_lang,
            "targetLang": target_lang,
        },
        timeout=timeout_seconds,
    )
    if not payload.get("ok"):
        raise VOTError(str(payload.get("error") or "VOT subtitle request failed"))

    source = _normalize_bridge_cues(payload.get("sourceCues"))
    target = _normalize_bridge_cues(payload.get("targetCues") or payload.get("cues"))

    # Backward compatibility with an older bridge during rolling upgrades.
    if not target:
        target = _vtt_fallback(payload, workdir)

    if not target:
        raise VOTError("VOT returned no translated subtitle cues")

    return {
        "source_cues": source,
        "target_cues": target,
        "route": payload.get("route"),
        "vot_js_version": payload.get("votJsVersion"),
        "source_language": payload.get("sourceLanguage"),
        "vot_request_language": payload.get("votRequestLanguage"),
        "selected": payload.get("selected") or {},
    }


def fetch_translated_subtitles(
    bridge_url: str,
    url: str,
    source_lang: str,
    target_lang: str,
    workdir: Path,
    timeout_seconds: int = 420,
) -> list[dict]:
    """Compatibility wrapper for older callers."""
    return fetch_vot_subtitle_bundle(
        bridge_url, url, source_lang, target_lang, workdir, timeout_seconds
    )["target_cues"]


def _cue_weight(cue: dict) -> float:
    text = str(cue.get("text") or "")
    word_count = len(re.findall(r"\w+", text, flags=re.UNICODE))
    duration = max(0.2, float(cue["end"]) - float(cue["start"]))
    return max(1.0, word_count * 0.8 + duration * 0.7)


def _sentence_units(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    units = [part.strip() for part in re.split(r"(?<=[.!?…])\s+", text) if part.strip()]
    if len(units) > 1:
        return units
    units = [part.strip() for part in re.split(r"(?<=[,;:])\s+|\s+[—–]\s+", text) if part.strip()]
    return units if units else [text]


def _partition_units(units: list[str], weights: list[float]) -> list[str]:
    n = len(weights)
    if n <= 0:
        return []
    if not units:
        return [""] * n
    if n == 1:
        return [" ".join(units).strip()]

    if len(units) < n:
        words = " ".join(units).split()
        if not words:
            return [""] * n
        if len(words) <= n:
            return (words + [""] * (n - len(words)))[:n]
        total_w = sum(weights) or float(n)
        out: list[str] = []
        cursor = 0
        remaining_words = len(words)
        remaining_weight = total_w
        for i, weight in enumerate(weights):
            remaining_slots = n - i
            if remaining_slots == 1:
                take = remaining_words
            else:
                ideal = round(remaining_words * (weight / max(remaining_weight, 1e-9)))
                take = max(1, min(ideal, remaining_words - (remaining_slots - 1)))
            out.append(" ".join(words[cursor : cursor + take]).strip())
            cursor += take
            remaining_words -= take
            remaining_weight -= weight
        return out

    unit_sizes = [max(1, len(u)) for u in units]
    total_size = sum(unit_sizes)
    total_weight = sum(weights) or float(n)
    desired = [total_size * (w / total_weight) for w in weights]

    out: list[str] = []
    cursor = 0
    for i in range(n):
        remaining_groups = n - i
        remaining_units = len(units) - cursor
        if remaining_groups == 1:
            out.append(" ".join(units[cursor:]).strip())
            break
        max_take = remaining_units - (remaining_groups - 1)
        target = desired[i]
        take = 1
        size = unit_sizes[cursor]
        while take < max_take:
            next_size = size + 1 + unit_sizes[cursor + take]
            if abs(next_size - target) <= abs(size - target):
                size = next_size
                take += 1
            else:
                break
        out.append(" ".join(units[cursor : cursor + take]).strip())
        cursor += take

    while len(out) < n:
        out.append("")
    return out[:n]


def _split_text_for_cues(text: str, cues: list[dict]) -> list[str]:
    if not cues:
        return []
    if len(cues) == 1:
        return [str(text or "").strip()]
    return _partition_units(_sentence_units(text), [_cue_weight(c) for c in cues])


def _interval_overlap(a: dict, b: dict) -> float:
    return max(0.0, min(float(a["end"]), float(b["end"])) - max(float(a["start"]), float(b["start"])))


def build_synced_phrase_pairs(source_cues: list[dict], target_cues: list[dict]) -> list[dict]:
    """Build one canonical study timeline from VOT source + target tracks.

    Every target cue contributes only to source cues it actually overlaps. If a
    translated cue spans several source cues, its text is split in chronological
    order. If several translated cues fall inside one source cue, they are joined.
    This handles both coarse and fine subtitle segmentation without mixing two
    unrelated timing systems.
    """
    source = _normalize_bridge_cues(source_cues)
    target = _normalize_bridge_cues(target_cues)
    if not source:
        return []

    contributions: dict[int, list[tuple[float, str]]] = defaultdict(list)

    for t in target:
        overlaps: list[tuple[int, float]] = []
        for i, s in enumerate(source):
            ov = _interval_overlap(s, t)
            if ov > 0.02:
                overlaps.append((i, ov))

        if not overlaps:
            t_mid = (float(t["start"]) + float(t["end"])) / 2
            nearest = min(
                range(len(source)),
                key=lambda i: abs(((float(source[i]["start"]) + float(source[i]["end"])) / 2) - t_mid),
            )
            s_mid = (float(source[nearest]["start"]) + float(source[nearest]["end"])) / 2
            if abs(s_mid - t_mid) <= 1.5:
                overlaps = [(nearest, 1.0)]

        if not overlaps:
            continue

        indexes = [i for i, _ in overlaps]
        if len(indexes) == 1:
            contributions[indexes[0]].append((float(t["start"]), str(t["text"]).strip()))
            continue

        src_slice = [source[i] for i in indexes]
        chunks = _split_text_for_cues(str(t["text"]), src_slice)
        for i, chunk in zip(indexes, chunks):
            if chunk.strip():
                contributions[i].append((float(t["start"]), chunk.strip()))

    pairs: list[dict] = []
    for i, s in enumerate(source):
        parts = [text for _, text in sorted(contributions.get(i, []), key=lambda x: x[0]) if text]
        # Avoid duplicate text when VOT exposes repeated/cumulative adjacent cues.
        compact: list[str] = []
        for text in parts:
            if compact and text == compact[-1]:
                continue
            if compact and text.startswith(compact[-1] + " "):
                text = text[len(compact[-1]):].strip()
            if text:
                compact.append(text)
        pairs.append({
            "start": float(s["start"]),
            "end": float(s["end"]),
            "source_text": str(s["text"]).strip(),
            "translated_text": " ".join(compact).strip(),
        })
    # Repair a common VOT segmentation artifact where the target track cuts a
    # sentence at a different point than the source track. Example:
    #   source 1: "Well, he's at camp all week."
    #   target 1: "Он в лагере на всю неделю. Жаль,"
    #   source 2: "I'm sorry you won't get to meet him."
    #   target 2: "что ты не сможешь с ним познакомиться."
    #
    # Timing alone assigns "Жаль," to the previous cue. If a completed sentence
    # is followed by a short dangling comma/colon fragment, move only that
    # fragment to the next translation. This is deliberately conservative.
    for i in range(len(pairs) - 1):
        current = str(pairs[i].get("translated_text") or "").strip()
        following = str(pairs[i + 1].get("translated_text") or "").strip()
        if not current or not following:
            continue

        match = re.match(
            r"^(?P<main>.+[.!?…])\s+(?P<tail>[^.!?…]{1,48}[,:;])$",
            current,
            flags=re.UNICODE,
        )
        if not match:
            continue

        tail = match.group("tail").strip()
        # Avoid moving long clauses; this is for short discourse fragments like
        # "Жаль,", "Но,", "Вообще-то," that were split at the wrong cue boundary.
        if len(re.findall(r"\w+", tail, flags=re.UNICODE)) > 5:
            continue

        pairs[i]["translated_text"] = match.group("main").strip()
        pairs[i + 1]["translated_text"] = f"{tail} {following}".strip()

    return pairs


def align_translations(source_phrases, translated_cues: list[dict]) -> int:
    """Legacy fallback when the bridge cannot return VOT source cues."""
    if not translated_cues or not source_phrases:
        return 0

    cues = sorted(translated_cues, key=lambda cue: (float(cue["start"]), float(cue["end"])))
    assignments: dict[int, list] = defaultdict(list)

    for phrase in source_phrases:
        p_start = float(phrase.start_time)
        p_end = float(phrase.end_time)
        p_mid = (p_start + p_end) / 2
        p_duration = max(0.001, p_end - p_start)
        best_idx: int | None = None
        best_score = -math.inf

        for idx, cue in enumerate(cues):
            c_start = float(cue["start"])
            c_end = float(cue["end"])
            c_duration = max(0.001, c_end - c_start)
            overlap = max(0.0, min(p_end, c_end) - max(p_start, c_start))
            if overlap <= 0:
                continue
            c_mid = (c_start + c_end) / 2
            score = overlap / p_duration * 2.0 + overlap / c_duration - abs(c_mid - p_mid) * 0.01
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx is None:
            nearest_idx = min(
                range(len(cues)),
                key=lambda idx: abs(((float(cues[idx]["start"]) + float(cues[idx]["end"])) / 2) - p_mid),
            )
            nearest = cues[nearest_idx]
            distance = abs(((float(nearest["start"]) + float(nearest["end"])) / 2) - p_mid)
            if distance <= 2.0:
                best_idx = nearest_idx

        if best_idx is not None:
            assignments[best_idx].append(phrase)

    updated = 0
    for cue_idx, phrases in assignments.items():
        cue_text = str(cues[cue_idx].get("text") or "").strip()
        if not cue_text:
            continue
        phrases.sort(key=lambda p: (float(p.start_time), float(p.end_time)))
        chunks = _partition_units(_sentence_units(cue_text), [
            max(1.0, len(re.findall(r"\w+", str(p.source_text), flags=re.UNICODE)) * 0.8 + max(0.2, p.end_time - p.start_time) * 0.7)
            for p in phrases
        ])
        for phrase, chunk in zip(phrases, chunks):
            if chunk.strip():
                phrase.translated_text = chunk.strip()
                updated += 1
    return updated
