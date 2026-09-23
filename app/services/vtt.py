import html
import re
from pathlib import Path

TIMING_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[\.,]\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2}[\.,]\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>")


def _seconds(ts: str) -> float:
    ts = ts.replace(",", ".")
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_vtt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    cues = []
    i = 0
    seen = set()

    while i < len(lines):
        match = TIMING_RE.search(lines[i])
        if not match:
            i += 1
            continue

        start = _seconds(match.group("start"))
        end = _seconds(match.group("end"))
        i += 1
        body = []
        while i < len(lines) and lines[i].strip():
            cleaned = TAG_RE.sub("", lines[i]).strip()
            if cleaned:
                body.append(cleaned)
            i += 1

        source_text = html.unescape(" ".join(body)).strip()
        source_text = re.sub(r"\s+", " ", source_text)
        key = (round(start, 3), round(end, 3), source_text)
        if source_text and key not in seen:
            seen.add(key)
            cues.append({"start": start, "end": end, "text": source_text})
        i += 1

    return cues
