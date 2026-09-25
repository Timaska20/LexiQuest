# Google Sheets + AnkiConnect setup (LexiQuest 0.11)

## 1. Google Cloud OAuth

1. Open Google Cloud Console and create/select a project.
2. Enable **Google Sheets API**.
3. Configure the OAuth consent screen for the Google account that owns or can edit the two spreadsheets.
4. Create an OAuth client of type **Web application**.
5. Add the exact redirect URI used by LexiQuest:

   `https://YOUR_LEXIQUEST_HOST/api/auth/google/callback`

6. Copy the Client ID and Client Secret to the VPS `.env`.

The application requests:

- `https://www.googleapis.com/auth/spreadsheets`
- `openid`
- `email`

`openid email` are used only so `/api/auth/google/status` can show which Google account is connected.

## 2. VPS .env

Keep all existing LexiQuest settings and append:

```env
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://YOUR_LEXIQUEST_HOST/api/auth/google/callback
GOOGLE_OAUTH_SCOPES=https://www.googleapis.com/auth/spreadsheets openid email
GOOGLE_PROGRESS_SHEET_ID=17Sfv37TCQx8Fw09c_8CGwzUG6iB2a-ZSY8rqVgP8GtM
GOOGLE_ANALYTICS_SHEET_ID=1o3ftf_R3K67meRjqA_6TgIo9W4wl0PfiM2bn8jjaeRo
GOOGLE_PROGRESS_TAB=Daily_Progress
GOOGLE_ANALYTICS_TAB=Weak_Spots_Log
GOOGLE_TIMEZONE=Asia/Almaty
```

Restart the API after changing `.env`:

```bash
docker compose up -d --build
```

Then open LexiQuest. The header will show **Подключить Google Sheets** until OAuth is completed.

Tokens are stored as `data/google_tokens.json` with restrictive file permissions. The file is ignored by Git.

## 3. Daily_Progress columns

LexiQuest matches common Russian/English header variants. Recommended canonical headers:

- `Date`
- `Status`
- `Video URL`
- `Grammar Topic`
- `Recommended Moments`
- `New Cards Count`
- `Notes`

Recommended moments can be written as:

```text
00:00–01:21: Завязка, 02:32–03:30: Финал
```

LexiQuest converts them to second ranges and keeps the full downloaded video. Daily ingestion uses the existing `Video`/`Job` worker pipeline.

## 4. Weak_Spots_Log

On daily completion LexiQuest appends vocabulary rows to `Weak_Spots_Log` for words that were looked up repeatedly or mined to Anki:

```text
Date | Vocabulary | Word | Context | Frequency | Active
```

Detailed session history is also stored locally in SQLite table `studysession`.

## 5. AnkiConnect on desktop

Install and run Anki Desktop + AnkiConnect. LexiQuest calls AnkiConnect directly from the browser; the VPS never calls its own localhost.

Default host:

```text
http://localhost:8765
```

For a LAN/Tailscale setup, set **AnkiConnect Host** in the LexiQuest UI to the reachable desktop address.

AnkiConnect must allow the LexiQuest web origin. A permissive development example is:

```json
"webCorsOriginList": ["*"]
```

For normal use prefer the exact LexiQuest origin instead of `*` when possible.

If AnkiConnect cannot be reached (for example while using a phone), LexiQuest stores the card in SQLite with `pending_anki`. Opening LexiQuest later on a desktop exposes the pending count and one-click synchronization.

## 6. Card format

Direct sync uses model:

`Basic (type in the answer)`

Fields:

- `Front`: source subtitle sentence with the mined word replaced by `[…]`
- `Back`: target word/meaning plus `[sound:...]` when an audio clip is available

The literal `{{type:...}}` template is intentionally not inserted inside a field value: Anki's type-in behavior belongs to the card model template itself. Existing LexiQuest `.apkg` export remains available as a fallback.
