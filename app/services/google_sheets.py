from __future__ import annotations

import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import settings

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_SCOPES = f"{SHEETS_SCOPE} openid email"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

_PROGRESS_HEADERS = {
    "date": ["date", "дата"],
    "status": ["status", "статус"],
    "video_title": ["video title", "video_title", "title", "название видео", "название"],
    "video_url": ["video url", "video_url", "youtube url", "youtube", "видео", "ссылка", "ссылка на видео"],
    "grammar_topic": ["murphy topic", "grammar topic", "grammar_topic", "grammar", "грамматика", "murphy", "мёрфи", "мерфи"],
    "notes": ["notes", "note", "заметки", "примечания", "описание", "description"],
    "recommended": ["key moments", "recommended moments", "recommended_moments", "моменты", "рекомендуемые моменты", "таймкоды", "timestamps"],
    "duration": ["duration", "длительность"],
    "new_cards": ["new cards count", "new_cards_count", "new cards", "новые карточки", "карточки"],
}

_ACTIVE_STATUSES = {"в процессе", "ожидает запуска", "in progress", "pending", "ready"}


class GoogleSheetsError(RuntimeError):
    pass


def _tokens_path() -> Path:
    return settings.google_tokens_path


def _state_path() -> Path:
    return settings.data_dir / "google_oauth_state.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, data: bytes | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise GoogleSheetsError(f"Google API HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise GoogleSheetsError(f"Google API unavailable: {exc.reason}") from exc


def configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret and settings.google_redirect_uri)


def build_login_url() -> str:
    if not configured():
        raise GoogleSheetsError("Google OAuth is not configured")
    state = secrets.token_urlsafe(32)
    _write_json(_state_path(), {"state": state, "created_at": datetime.utcnow().isoformat()})
    scopes = settings.google_oauth_scopes.strip() or DEFAULT_SCOPES
    query = urllib.parse.urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def exchange_code(code: str, state: str) -> dict[str, Any]:
    expected = _read_json(_state_path()).get("state")
    if not expected or not secrets.compare_digest(str(expected), str(state or "")):
        raise GoogleSheetsError("Invalid or expired OAuth state")
    form = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()
    token = _http_json(
        TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form,
    )
    old = _read_json(_tokens_path())
    if not token.get("refresh_token") and old.get("refresh_token"):
        token["refresh_token"] = old["refresh_token"]
    token["obtained_at"] = int(datetime.now().timestamp())
    _write_json(_tokens_path(), token)
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        pass
    return token


def _refresh_token(tokens: dict[str, Any]) -> dict[str, Any]:
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise GoogleSheetsError("Google refresh_token is missing; reconnect Google Sheets")
    form = urllib.parse.urlencode(
        {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
    ).encode()
    new = _http_json(
        TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form,
    )
    tokens.update(new)
    tokens["refresh_token"] = refresh
    tokens["obtained_at"] = int(datetime.now().timestamp())
    _write_json(_tokens_path(), tokens)
    return tokens


def access_token() -> str:
    tokens = _read_json(_tokens_path())
    if not tokens.get("access_token"):
        raise GoogleSheetsError("Google Sheets is not authorized")
    obtained = int(tokens.get("obtained_at") or 0)
    expires = int(tokens.get("expires_in") or 3600)
    if datetime.now().timestamp() >= obtained + max(60, expires - 120):
        tokens = _refresh_token(tokens)
    return str(tokens["access_token"])


def _google_json(url: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {access_token()}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        return _http_json(url, method=method, headers=headers, data=payload)
    except GoogleSheetsError as exc:
        if "HTTP 401" not in str(exc):
            raise
        _refresh_token(_read_json(_tokens_path()))
        headers["Authorization"] = f"Bearer {access_token()}"
        return _http_json(url, method=method, headers=headers, data=payload)


def auth_status() -> dict[str, Any]:
    if not configured() or not _tokens_path().exists():
        return {"authorized": False, "email": None}
    try:
        info = _google_json(USERINFO_URL)
        return {"authorized": True, "email": info.get("email")}
    except Exception:
        try:
            access_token()
            return {"authorized": True, "email": None}
        except Exception:
            return {"authorized": False, "email": None}


def _sheet_values(spreadsheet_id: str, range_name: str) -> list[list[Any]]:
    encoded = urllib.parse.quote(range_name, safe="")
    data = _google_json(f"{SHEETS_API}/{spreadsheet_id}/values/{encoded}?majorDimension=ROWS")
    return data.get("values") or []


def _normalize_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold().replace("_", " "))


def _column_map(headers: list[Any]) -> dict[str, int]:
    normalized = [_normalize_header(x) for x in headers]
    result: dict[str, int] = {}
    for logical, candidates in _PROGRESS_HEADERS.items():
        for candidate in candidates:
            candidate = _normalize_header(candidate)
            if candidate in normalized:
                result[logical] = normalized.index(candidate)
                break
    return result


def _cell(row: list[Any], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx] or "").strip()


def _parse_date(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if match:
        y, m, d = map(int, match.groups())
        try:
            return datetime(y, m, d).date().isoformat()
        except ValueError:
            return None
    return None


def _youtube_url(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = re.search(
        r"https?://(?:(?:www|m)\.)?(?:youtube\.com|youtu\.be)/[^\s,;]+",
        raw,
        re.I,
    )
    return match.group(0).rstrip(").]") if match else ""


def _seconds(value: str) -> int:
    parts = [int(x) for x in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(value)


def parse_recommended_moments(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]\s*"
        r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)"
        r"(?:\s*[:\-–—]\s*(?P<title>[^,;\n]+))?"
    )
    result = []
    for index, match in enumerate(pattern.finditer(text)):
        try:
            start = _seconds(match.group("start"))
            end = _seconds(match.group("end"))
        except ValueError:
            continue
        if end <= start:
            continue
        title = (match.group("title") or "").strip()
        result.append({"start": start, "end": end, "title": title or f"Момент {index + 1}"})
    return result


def get_daily_task() -> dict[str, Any]:
    rows = _sheet_values(settings.google_progress_sheet_id, f"{settings.google_progress_tab}!A:Z")
    if not rows:
        raise GoogleSheetsError("Daily_Progress is empty")
    headers = rows[0]
    cols = _column_map(headers)

    # Canonical English Progress layout fallback:
    # A Date | B Day of Week | C Murphy Topic | D Video Title |
    # E Video URL | F Key Moments | G Duration | H Status |
    # I New Cards Count | J Notes
    canonical = {
        "date": 0,
        "grammar_topic": 2,
        "video_title": 3,
        "video_url": 4,
        "recommended": 5,
        "duration": 6,
        "status": 7,
        "new_cards": 8,
        "notes": 9,
    }
    if len(headers) >= 10:
        normalized_headers = [_normalize_header(x) for x in headers]
        if normalized_headers[:10] == [
            "date", "day of week", "murphy topic", "video title", "video url",
            "key moments", "duration", "status", "new cards count", "notes",
        ]:
            cols.update(canonical)

    if "date" not in cols:
        raise GoogleSheetsError(
            f"Daily_Progress: Date/Дата column not found. Headers: {headers}"
        )

    today = datetime.now(ZoneInfo(settings.google_timezone)).date().isoformat()
    candidates: list[dict[str, Any]] = []
    for sheet_row, row in enumerate(rows[1:], start=2):
        date_iso = _parse_date(_cell(row, cols.get("date")))
        status = _cell(row, cols.get("status"))
        combined = " | ".join(
            value for value in (
                _cell(row, cols.get("video_url")),
                _cell(row, cols.get("grammar_topic")),
                _cell(row, cols.get("recommended")),
                _cell(row, cols.get("notes")),
            ) if value
        )
        raw_video_url = _cell(row, cols.get("video_url"))
        video_url = _youtube_url(raw_video_url) or _youtube_url(combined)
        grammar = _cell(row, cols.get("grammar_topic"))
        video_title = _cell(row, cols.get("video_title"))
        recommended_source = " | ".join(
            x for x in (_cell(row, cols.get("recommended")), _cell(row, cols.get("notes"))) if x
        )
        candidates.append(
            {
                "sheet_row": sheet_row,
                "date": date_iso or _cell(row, cols.get("date")),
                "status": status,
                "video_url": video_url,
                "video_title": video_title,
                "grammar_topic": grammar,
                "notes": _cell(row, cols.get("notes")),
                "recommended_moments": parse_recommended_moments(recommended_source),
            }
        )

    exact = [x for x in candidates if x["date"] == today]
    if exact:
        task = exact[-1]
    else:
        active = [x for x in candidates if _normalize_header(x["status"]) in _ACTIVE_STATUSES]
        if not active:
            raise GoogleSheetsError("No task for today and no active Daily_Progress row")
        task = active[-1]
    if not task["video_url"]:
        row = rows[task["sheet_row"] - 1] if task["sheet_row"] - 1 < len(rows) else []
        detected = _cell(row, cols.get("video_url"))
        raise GoogleSheetsError(
            f"Daily_Progress row {task['sheet_row']} has no YouTube URL. "
            f"Detected Video URL cell: {detected!r}; headers: {headers}"
        )
    return task


def _column_letter(index: int) -> str:
    index += 1
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _update_values(spreadsheet_id: str, range_name: str, values: list[list[Any]]) -> None:
    encoded = urllib.parse.quote(range_name, safe="")
    url = f"{SHEETS_API}/{spreadsheet_id}/values/{encoded}?valueInputOption=USER_ENTERED"
    _google_json(url, method="PUT", body={"range": range_name, "majorDimension": "ROWS", "values": values})


def complete_daily_task(date_iso: str, mined_cards_count: int, time_spent_seconds: int) -> dict[str, Any]:
    rows = _sheet_values(settings.google_progress_sheet_id, f"{settings.google_progress_tab}!A:Z")
    if not rows:
        raise GoogleSheetsError("Daily_Progress is empty")
    headers = rows[0]
    cols = _column_map(headers)
    wanted = None
    for sheet_row, row in enumerate(rows[1:], start=2):
        if _parse_date(_cell(row, cols.get("date"))) == date_iso:
            wanted = (sheet_row, row)
            break
    if not wanted:
        raise GoogleSheetsError(f"Daily_Progress row for {date_iso} not found")
    sheet_row, row = wanted
    updates: list[tuple[int, Any]] = []
    if "status" in cols:
        updates.append((cols["status"], "Посмотрел"))
    if "new_cards" in cols:
        updates.append((cols["new_cards"], mined_cards_count))
    if "notes" in cols:
        idx = cols["notes"]
        old_notes = _cell(row, idx)
        minutes = max(1, round(time_spent_seconds / 60))
        marker = f"Просмотрено в LexiQuest ({minutes} мин)"
        notes = old_notes if marker in old_notes else (f"{old_notes}\n{marker}".strip())
        updates.append((idx, notes))
    for idx, value in updates:
        letter = _column_letter(idx)
        _update_values(
            settings.google_progress_sheet_id,
            f"{settings.google_progress_tab}!{letter}{sheet_row}",
            [[value]],
        )
    return {"sheet_row": sheet_row, "updated": len(updates)}


def append_weak_spots(date_iso: str, events: list[dict[str, Any]]) -> int:
    rows = []
    for event in events:
        word = str(event.get("word") or "").strip()
        frequency = int(event.get("frequency") or 1)
        mined = bool(event.get("mined"))
        if not word or (frequency < 2 and not mined):
            continue
        rows.append(
            [
                date_iso,
                "Vocabulary",
                word,
                str(event.get("context") or "")[:1000],
                max(1, frequency),
                "Active",
            ]
        )
    if not rows:
        return 0
    range_name = f"{settings.google_analytics_tab}!A:F"
    encoded = urllib.parse.quote(range_name, safe="")
    url = (
        f"{SHEETS_API}/{settings.google_analytics_sheet_id}/values/{encoded}:append"
        "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    _google_json(url, method="POST", body={"range": range_name, "majorDimension": "ROWS", "values": rows})
    return len(rows)
