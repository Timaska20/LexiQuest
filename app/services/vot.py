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


def _is_terminal(text: str) -> bool:
    return bool(re.search(r"[.!?…][\"'»”)]*$", str(text or "").strip()))


def _is_dangling_fragment(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    words = re.findall(r"\w+", value, flags=re.UNICODE)
    if len(words) > 5:
        return False
    if re.search(r"[,;:][\"'»”)]*$", value):
        return True
    lowered = value.casefold()
    dangling_starts = (
        "и ", "но ", "а ", "или ", "что ", "чтобы ", "потому что ",
        "если ", "хотя ", "когда ", "пока ", "жаль", "вообще-то",
    )
    return any(lowered == item.strip() or lowered.startswith(item) for item in dangling_starts)


def _target_units(target: list[dict]) -> list[dict]:
    """Split translated cues into punctuation-aware units with approximate time spans."""
    out: list[dict] = []
    for cue in target:
        text = str(cue.get("text") or "").strip()
        units = _sentence_units(text)
        if not units:
            continue
        start = float(cue["start"])
        end = float(cue["end"])
        duration = max(0.01, end - start)
        weights = [max(1, len(re.findall(r"\w+", u, flags=re.UNICODE))) for u in units]
        total = float(sum(weights)) or 1.0
        cursor = start
        for idx, (unit, weight) in enumerate(zip(units, weights)):
            if idx == len(units) - 1:
                unit_end = end
            else:
                unit_end = cursor + duration * (weight / total)
            out.append({
                "text": unit.strip(),
                "start": cursor,
                "end": max(cursor + 0.01, unit_end),
            })
            cursor = unit_end
            total -= weight
            duration = max(0.01, end - cursor)
    return out


def _group_alignment_score(source_cue: dict, units: list[dict]) -> float:
    """Score one monotonic assignment of translated units to one source cue."""
    if not units:
        return -3.0

    s_start = float(source_cue["start"])
    s_end = float(source_cue["end"])
    s_duration = max(0.05, s_end - s_start)
    s_mid = (s_start + s_end) / 2

    t_start = float(units[0]["start"])
    t_end = float(units[-1]["end"])
    t_duration = max(0.05, t_end - t_start)
    t_mid = (t_start + t_end) / 2

    overlap = max(0.0, min(s_end, t_end) - max(s_start, t_start))
    overlap_score = (overlap / s_duration) * 3.0 + (overlap / t_duration) * 2.0
    distance_penalty = min(6.0, abs(s_mid - t_mid)) * 0.45

    source_text = str(source_cue.get("text") or "").strip()
    target_text = " ".join(str(u.get("text") or "").strip() for u in units).strip()

    boundary_score = 0.0
    source_terminal = _is_terminal(source_text)
    target_terminal = _is_terminal(target_text)
    if source_terminal and target_terminal:
        boundary_score += 4.0
    elif source_terminal and not target_terminal:
        boundary_score -= 3.0
    elif not source_terminal and target_terminal:
        boundary_score += 0.5

    dangling_penalty = 5.0 if _is_dangling_fragment(target_text) else 0.0

    source_words = max(1, len(re.findall(r"\w+", source_text, flags=re.UNICODE)))
    target_words = max(1, len(re.findall(r"\w+", target_text, flags=re.UNICODE)))
    ratio = target_words / source_words
    length_score = max(-2.0, 1.5 - abs(math.log(max(0.15, min(6.0, ratio)))) * 1.1)

    # Keep the optimizer local: assignments far outside the source timing window
    # are possible only with a strong sentence-boundary reason, not by default.
    locality_penalty = 0.0
    if t_end < s_start - 2.5 or t_start > s_end + 2.5:
        locality_penalty = 8.0

    return overlap_score + boundary_score + length_score - distance_penalty - dangling_penalty - locality_penalty


def _dp_refine_translation_alignment(source: list[dict], target: list[dict]) -> list[str]:
    """Monotonic DP refinement using time + sentence boundaries + local context.

    Source cue timings stay canonical. The DP only decides which translated
    sentence/clause units belong to each source cue.
    """
    units = _target_units(target)
    if not source:
        return []
    if not units:
        return [""] * len(source)

    n = len(source)
    m = len(units)
    max_take = 4
    neg_inf = -10**12

    # dp[i][j] = best score after assigning first j target units to first i source cues.
    dp = [[neg_inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0

    for i in range(n):
        for j in range(m + 1):
            base = dp[i][j]
            if base <= neg_inf / 2:
                continue

            # Allow an untranslated source cue, but penalize it.
            if base - 3.0 > dp[i + 1][j]:
                dp[i + 1][j] = base - 3.0
                back[i + 1][j] = (j, 0)

            for take in range(1, min(max_take, m - j) + 1):
                group = units[j : j + take]
                score = _group_alignment_score(source[i], group)

                # Discourage swallowing many independently punctuated sentences
                # into one short source cue unless timings strongly support it.
                terminal_inside = sum(1 for u in group[:-1] if _is_terminal(u["text"]))
                score -= terminal_inside * 1.2

                candidate = base + score
                if candidate > dp[i + 1][j + take]:
                    dp[i + 1][j + take] = candidate
                    back[i + 1][j + take] = (j, take)

    # Prefer consuming all units; if impossible, choose the furthest consumed
    # state with a small penalty for leftovers.
    best_j = max(range(m + 1), key=lambda j: dp[n][j] - (m - j) * 4.0)
    assignments: list[list[dict]] = [[] for _ in range(n)]
    i, j = n, best_j
    while i > 0:
        step = back[i][j]
        if step is None:
            i -= 1
            continue
        prev_j, take = step
        if take:
            assignments[i - 1] = units[prev_j:j]
        j = prev_j
        i -= 1

    # Rare leftover units are attached to the final source cue rather than lost.
    if best_j < m and assignments:
        assignments[-1].extend(units[best_j:])

    return [
        " ".join(str(u.get("text") or "").strip() for u in group if str(u.get("text") or "").strip()).strip()
        for group in assignments
    ]


def build_synced_phrase_pairs(source_cues: list[dict], target_cues: list[dict]) -> list[dict]:
    """Build one canonical study timeline from VOT source + target tracks.

    Source timings are authoritative. Target text is aligned monotonically with
    dynamic programming using temporal overlap, sentence/clause boundaries,
    length balance and penalties for dangling fragments. This handles different
    EN/RU subtitle segmentation without changing audio/card boundaries.
    """
    source = _normalize_bridge_cues(source_cues)
    target = _normalize_bridge_cues(target_cues)
    if not source:
        return []

    refined = _dp_refine_translation_alignment(source, target)

    pairs: list[dict] = []
    for i, s in enumerate(source):
        pairs.append({
            "start": float(s["start"]),
            "end": float(s["end"]),
            "source_text": str(s["text"]).strip(),
            "translated_text": refined[i] if i < len(refined) else "",
        })

    # Conservative safety-net for a common remaining boundary artifact:
    # "Sentence. Жаль," / "что ..." -> "Sentence." / "Жаль, что ..."
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
