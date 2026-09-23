import http from "node:http";
import { readFile } from "node:fs/promises";
import VOTClient from "@vot.js/node";
import { VOTWorkerProvider } from "@vot.js/core/providers/votworker";
import { getVideoData } from "@vot.js/node/utils/videoData";
import { fetch as undiciFetch } from "undici";

const PORT = Number(process.env.PORT || 3100);
const POLL_TIMEOUT_MS = Number(process.env.VOT_POLL_TIMEOUT_MS || 180000);
const POLL_INTERVAL_MS = Number(process.env.VOT_POLL_INTERVAL_MS || 5000);
const MAX_BODY_BYTES = 32 * 1024;
const version = (await readFile("/app/vot-version.txt", "utf8")).trim();

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function withTimeout(promise, ms, label) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timed out after ${Math.round(ms / 1000)}s`)), ms);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
const LANG_ALIASES = new Map([["kz", "kk"], ["kaz", "kk"], ["eng", "en"], ["rus", "ru"]]);
const normalizeLang = (value) => {
  const raw = String(value || "").trim().toLowerCase().replaceAll("_", "-");
  if (!raw) return raw;
  const [base, ...rest] = raw.split("-");
  const canonical = LANG_ALIASES.get(base) || base;
  return rest.length ? `${canonical}-${rest.join("-")}` : canonical;
};
const langMatches = (a, b) => {
  const aa = normalizeLang(a);
  const bb = normalizeLang(b);
  return aa === bb || aa.split("-")[0] === bb.split("-")[0];
};

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  res.end(body);
}

async function readJson(req) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error("Request body is too large");
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : {};
}

function validateSourceUrl(value) {
  const u = new URL(String(value || ""));
  if (!["http:", "https:"].includes(u.protocol)) throw new Error("Only http/https source URLs are allowed");
  return u.toString();
}

function selectSubtitle(subtitles, sourceLang, targetLang) {
  if (!Array.isArray(subtitles)) return null;

  const source = normalizeLang(sourceLang);
  const target = normalizeLang(targetLang);
  const autoSource = source === "auto";

  // AUTO means: let Yandex/VOT detect speech, then choose the translated pair
  // that VOT itself associates with the requested target language. We never
  // persist "auto" as the lesson language after success; the worker stores the
  // detected language from this selected row.
  if (autoSource) {
    const translatedPair = subtitles.find(
      (s) =>
        s.url &&
        s.translatedUrl &&
        langMatches(s.translatedLanguage, target) &&
        normalizeLang(s.language) !== "auto",
    );
    if (translatedPair) {
      return {
        sourceUrl: translatedPair.url,
        targetUrl: translatedPair.translatedUrl,
        language: translatedPair.translatedLanguage,
        translatedFromLanguage: translatedPair.language,
        sourceLanguage: translatedPair.language,
        translated: true,
      };
    }

    // Some VOT responses expose source and translated URLs on separate rows.
    const translatedRow = subtitles.find(
      (s) => s.translatedUrl && langMatches(s.translatedLanguage, target),
    );
    if (translatedRow) {
      const detected = normalizeLang(translatedRow.language || "");
      const sourceTrack = subtitles.find(
        (s) => s.url && detected && langMatches(s.language, detected),
      );
      if (sourceTrack?.url && detected && detected !== "auto") {
        return {
          sourceUrl: sourceTrack.url,
          targetUrl: translatedRow.translatedUrl,
          language: translatedRow.translatedLanguage,
          translatedFromLanguage: detected,
          sourceLanguage: detected,
          translated: true,
        };
      }
    }
    return null;
  }

  if (langMatches(source, target)) {
    const same = subtitles.find((s) => langMatches(s.language, source) && s.url);
    if (same) {
      return {
        sourceUrl: same.url,
        targetUrl: same.url,
        language: same.language,
        translatedFromLanguage: same.language,
        sourceLanguage: same.language,
        translated: false,
      };
    }
  }

  const exactPair = subtitles.find(
    (s) =>
      langMatches(s.language, source) &&
      langMatches(s.translatedLanguage, target) &&
      s.url &&
      s.translatedUrl,
  );
  if (exactPair) {
    return {
      sourceUrl: exactPair.url,
      targetUrl: exactPair.translatedUrl,
      language: exactPair.translatedLanguage,
      translatedFromLanguage: exactPair.language,
      sourceLanguage: exactPair.language,
      translated: true,
    };
  }

  const safeTranslated = subtitles.find(
    (s) =>
      langMatches(s.language, source) &&
      langMatches(s.translatedLanguage, target) &&
      s.translatedUrl,
  );
  if (safeTranslated) {
    const sourceTrack = subtitles.find((s) => langMatches(s.language, source) && s.url);
    return {
      sourceUrl: safeTranslated.url || sourceTrack?.url || null,
      targetUrl: safeTranslated.translatedUrl,
      language: safeTranslated.translatedLanguage,
      translatedFromLanguage: safeTranslated.language,
      sourceLanguage: safeTranslated.language,
      translated: true,
    };
  }

  return null;
}
function availablePairs(subtitles) {
  if (!Array.isArray(subtitles)) return [];
  return subtitles.map((s) => ({
    language: s.language || null,
    translatedLanguage: s.translatedLanguage || null,
    hasSource: Boolean(s.url),
    hasTranslation: Boolean(s.translatedUrl),
  }));
}

function parseVttTimestamp(value) {
  const parts = String(value).trim().replace(",", ".").split(":");
  if (parts.length === 3) return Number(parts[0]) * 3600 + Number(parts[1]) * 60 + Number(parts[2]);
  if (parts.length === 2) return Number(parts[0]) * 60 + Number(parts[1]);
  return Number(parts[0]);
}

function stripTags(text) {
  return String(text || "")
    .replace(/<[^>]*>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}

function parseVtt(text) {
  const lines = String(text).replace(/\r/g, "").split("\n");
  const cues = [];
  for (let i = 0; i < lines.length; i += 1) {
    const m = lines[i].match(/((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})/);
    if (!m) continue;
    const start = parseVttTimestamp(m[1]);
    const end = parseVttTimestamp(m[2]);
    const body = [];
    i += 1;
    while (i < lines.length && lines[i].trim()) {
      body.push(lines[i]);
      i += 1;
    }
    const cueText = stripTags(body.join(" "));
    if (cueText && Number.isFinite(start) && Number.isFinite(end) && end > start) {
      cues.push({ start, end, text: cueText });
    }
  }
  return cues;
}

function parseSubtitlePayload(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return [];

  try {
    const data = JSON.parse(trimmed);
    const rows = Array.isArray(data)
      ? data
      : data?.subtitles ?? data?.body ?? data?.transcript?.body;
    if (Array.isArray(rows)) {
      return rows.flatMap((row) => {
        const startRaw = Number(row.startMs ?? row.start_ms ?? row.start ?? 0);
        const durationRaw = Number(row.durationMs ?? row.duration_ms ?? row.duration ?? 0);
        const endRaw = Number(row.endMs ?? row.end_ms ?? row.end ?? 0);
        let start;
        let end;

        // Yandex subtitle JSON normally uses milliseconds, but tolerate both
        // {startMs,durationMs} and {startMs,endMs} forms.
        const hasMs =
          row.startMs !== undefined || row.start_ms !== undefined ||
          row.durationMs !== undefined || row.duration_ms !== undefined ||
          row.endMs !== undefined || row.end_ms !== undefined;
        if (hasMs) {
          start = startRaw / 1000;
          end = endRaw > startRaw
            ? endRaw / 1000
            : (startRaw + durationRaw) / 1000;
        } else {
          start = startRaw;
          end = endRaw > startRaw ? endRaw : (durationRaw > 0 ? start + durationRaw : start);
        }

        const cueText = stripTags(row.text ?? row.line ?? row.content ?? row.subtitle ?? "");
        return cueText && Number.isFinite(start) && Number.isFinite(end) && end > start
          ? [{ start, end, text: cueText }]
          : [];
      });
    }
  } catch (_) {
    // Not JSON; try VTT/SRT below.
  }

  return parseVtt(trimmed);
}

async function downloadCues(url) {
  const response = await fetch(url, {
    headers: {
      "user-agent": `LexiQuest-Cake/0.10 vot.js/${version}`,
      accept: "application/json,text/vtt,text/plain,*/*",
    },
    signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) throw new Error(`Subtitle download failed: HTTP ${response.status}`);
  const text = await response.text();
  const cues = parseSubtitlePayload(text);
  if (!cues.length) throw new Error("Subtitle response contained no usable cues");
  return cues;
}

async function requestSubtitles({ url, sourceLang, targetLang }) {
  const sourceUrl = validateSourceUrl(url);
  const source = normalizeLang(sourceLang || "auto");
  const target = normalizeLang(targetLang || "ru");
  // The VOT/Yandex pipeline can auto-detect source speech. For Kazakh this is
  // more reliable than sending a country-code-like value and also works for
  // videos that have no site subtitles. We still REQUIRE the returned source
  // subtitle track to be Kazakh before Cake accepts it.
  const sourceBase = source.split("-")[0];
  // Current VOT request language interface reliably exposes auto/ru/en. For a
  // manually selected language such as kk/es/de, ask VOT to auto-detect but
  // validate the returned track against the user's explicit source choice.
  const nativeRequestLangs = new Set(["auto", "ru", "en"]);
  const requestSource = nativeRequestLangs.has(sourceBase) ? sourceBase : "auto";
  console.log(`[VOT] language wanted=${source}->${target}, request=${requestSource}->${target}`);

  const videoData = await withTimeout(getVideoData(sourceUrl), 60000, "getVideoData");
  if (!videoData) throw new Error("vot.js could not resolve video data for this URL");

  // vot.js/node may create an npm-undici Dispatcher. Node 22's built-in fetch
  // ships its own Undici version, so mixing the two can fail with
  // UND_ERR_INVALID_ARG / "invalid onRequestStart method". Keep dispatcher and
  // fetch in the same npm-undici implementation.
  const tracedFetch = async (input, init = {}) => {
    const requestUrl = typeof input === "string" ? input : input?.url || String(input);
    const headers = new Headers(init.headers || {});
    headers.delete("sec-fetch-mode");

    const cleanInit = {
      ...init,
      headers: Object.fromEntries(headers.entries()),
    };

    try {
      const response = await undiciFetch(requestUrl, cleanInit);
      console.log(`[VOT HTTP] ${cleanInit.method || "GET"} ${response.status} ${requestUrl}`);
      return response;
    } catch (error) {
      console.warn(`[VOT HTTP] FETCH FAILED ${cleanInit.method || "GET"} ${requestUrl}`);
      console.warn(`[VOT HTTP] message=${error?.message || error}`);
      console.warn(`[VOT HTTP] cause=${error?.cause?.message || "none"}`);
      console.warn(`[VOT HTTP] code=${error?.cause?.code || "none"}`);
      throw error;
    }
  };

  const describeError = (error) => {
    const message = error instanceof Error ? error.message : String(error);
    const raw = error?.data?.data ?? error?.data;
    let detail = "";
    if (typeof raw === "string") detail = raw;
    else if (raw && typeof raw === "object" && !(raw instanceof ArrayBuffer)) {
      try { detail = JSON.stringify(raw); } catch (_) {}
    }
    return detail && detail !== "{}" ? `${message} | ${detail}` : message;
  };

  const routeDefs = [
    {
      name: "direct",
      create: () => new VOTClient({
        requestLang: requestSource,
        responseLang: target,
        fetchFn: tracedFetch,
      }),
    },
    {
      name: "proxy-official",
      create: () => new VOTClient({
        host: process.env.VOT_WORKER_HOST || "vot-new.toil-dump.workers.dev",
        provider: VOTWorkerProvider,
        requestLang: requestSource,
        responseLang: target,
        fetchFn: tracedFetch,
      }),
    },
    {
      name: "proxy-kload",
      create: () => new VOTClient({
        host: "vot-worker.kload.workers.dev",
        provider: VOTWorkerProvider,
        requestLang: requestSource,
        responseLang: target,
        fetchFn: tracedFetch,
      }),
    },
  ];

  let client = null;
  let routeName = null;
  let firstInfo = null;
  const routeErrors = [];

  for (const route of routeDefs) {
    const candidate = route.create();
    try {
      console.log(`[VOT] testing route=${route.name}`);
      const info = await withTimeout(
        candidate.getSubtitles({ videoData, requestLang: requestSource }),
        30000,
        `getSubtitles(${route.name})`,
      );
      client = candidate;
      routeName = route.name;
      firstInfo = info;
      console.log(`[VOT] selected route=${route.name}`);
      break;
    } catch (error) {
      const detail = describeError(error);
      routeErrors.push(`${route.name}: ${detail}`);
      console.warn(`[VOT] route=${route.name} failed: ${detail}`);
    }
  }

  if (!client) {
    throw new Error(`All VOT routes failed. ${routeErrors.join(" || ")}`);
  }

  const started = Date.now();
  let translationTriggered = false;
  let lastSubtitles = [];
  let lastTriggerError = null;
  let pendingInfo = firstInfo;

  while (Date.now() - started < POLL_TIMEOUT_MS) {
    const info = pendingInfo ?? (await withTimeout(
      client.getSubtitles({ videoData, requestLang: requestSource }),
      30000,
      `getSubtitles(${routeName})`,
    ));
    pendingInfo = null;
    lastSubtitles = info?.subtitles || [];

    const selected = selectSubtitle(lastSubtitles, source, target);
    if (!selected && (source === "auto" || sourceBase === "kk")) {
      console.log(`[VOT] waiting for matching source/translation; requested=${source}->${target} available=${JSON.stringify(availablePairs(lastSubtitles))}`);
    }
    if (selected?.targetUrl) {
      const [sourceCues, targetCues] = await Promise.all([
        selected.sourceUrl ? downloadCues(selected.sourceUrl) : Promise.resolve([]),
        downloadCues(selected.targetUrl),
      ]);
      console.log(
        `[VOT] subtitles ready route=${routeName}, source=${sourceCues.length}, target=${targetCues.length}`,
      );
      return {
        ok: true,
        provider: "vot.js",
        route: routeName,
        votJsVersion: version,
        sourceLanguage: selected.sourceLanguage || selected.translatedFromLanguage || source,
        votRequestLanguage: requestSource,
        targetLanguage: target,
        selected,
        sourceCues,
        targetCues,
        // Compatibility for older Cake workers.
        cues: targetCues,
      };
    }

    if (!translationTriggered && !langMatches(source, target)) {
      translationTriggered = true;
      try {
        console.log(`[VOT] triggering translation route=${routeName}`);
        await withTimeout(
          client.translateVideo({
            videoData,
            requestLang: requestSource,
            responseLang: target,
            extraOpts: { videoTitle: videoData.title },
          }),
          90000,
          `translateVideo(${routeName})`,
        );
      } catch (error) {
        lastTriggerError = describeError(error);
        console.warn(`[VOT] translation trigger route=${routeName}: ${lastTriggerError}`);
      }
    }

    await sleep(POLL_INTERVAL_MS);
  }

  const available = availablePairs(lastSubtitles);
  const suffix = lastTriggerError ? ` Trigger warning: ${lastTriggerError}` : "";
  const sourceHint = sourceBase === "kk"
    ? " VOT did not produce a Kazakh (kk) source track; Cake refused to substitute another language."
    : source === "auto"
      ? " VOT auto-detection did not produce a usable source+translation pair."
      : "";
  throw new Error(
    `No ${source}->${target} translated subtitle track became available via ${routeName} within ${Math.round(POLL_TIMEOUT_MS / 1000)}s. ` +
      `Available tracks: ${JSON.stringify(available)}.${suffix}${sourceHint}`,
  );
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === "GET" && req.url === "/health") {
      return sendJson(res, 200, {
        ok: true,
        service: "lexiquest-vot-bridge",
        votJsVersion: version,
        node: process.version,
      });
    }

    if (req.method === "POST" && req.url === "/subtitles") {
      const body = await readJson(req);
      if (!body.url) return sendJson(res, 400, { ok: false, error: "url is required" });
      const result = await requestSubtitles(body);
      return sendJson(res, 200, result);
    }

    return sendJson(res, 404, { ok: false, error: "Not found" });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[VOT] ${message}`);
    return sendJson(res, 502, { ok: false, error: message, votJsVersion: version });
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`LexiQuest VOT bridge listening on :${PORT}; vot.js ${version}`);
});
