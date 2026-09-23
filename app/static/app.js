const $ = (id) => document.getElementById(id);

const state = {
  videos: [],
  selectedVideo: null,
  phrases: [],
  flashcards: [],
  currentPhraseIndex: -1,
  looping: false,
  mode: 'watch',
  showTranslation: true,
  addMode: 'url',
  pollTimer: null,
  dictionaryPhraseId: null,
  stats: {lookups: 0, phraseJumps: 0, loops: 0},
  seenPhraseIds: new Set(),
  swipeStart: null,
};

const player = $('player');
const SPEEDS = [0.75, 1, 1.15, 1.25];

function normalizeLangCode(value, fallback = 'en') {
  const raw = String(value || fallback).trim().toLowerCase().replaceAll('_', '-');
  const parts = raw.split('-');
  const aliases = {kz:'kk', kaz:'kk', eng:'en', rus:'ru'};
  parts[0] = aliases[parts[0]] || parts[0];
  return parts.join('-') || fallback;
}

function fmtTime(sec) {
  sec = Number(sec || 0);
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, '0')}`;
}

function fmtBytes(bytes) {
  if (!bytes) return '';
  const mb = bytes / 1024 / 1024;
  return `${mb.toFixed(mb > 100 ? 0 : 1)} MB`;
}

function setVideoMeta(v) {
  if (!v) return;
  $('videoMeta').textContent = `${String(v.source_lang || '?').toUpperCase()} → ${String(v.target_lang || '?').toUpperCase()} · ${v.height ? v.height + 'p · ' : ''}${fmtBytes(v.file_size_bytes)}`;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}

function closeDialog(id) {
  const d = $(id);
  if (d?.open) d.close();
}

function clearSelectedLesson() {
  player.pause();
  player.removeAttribute('src');
  player.load();
  state.selectedVideo = null;
  state.phrases = [];
  state.flashcards = [];
  state.currentPhraseIndex = -1;
  state.dictionaryPhraseId = null;
  state.looping = false;
  state.mode = 'watch';
  state.seenPhraseIds = new Set();
  state.stats = {lookups: 0, phraseJumps: 0, loops: 0};
  clearOverlay();
  $('playerView').classList.add('hidden');
  $('emptyState').classList.remove('hidden');
  renderLibrary();
}

async function api(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const type = res.headers.get('content-type') || '';
  return type.includes('application/json') ? res.json() : res;
}

async function loadVideos() {
  try {
    state.videos = await api('/api/videos');
    renderLibrary();
    if (state.selectedVideo) {
      const fresh = state.videos.find(v => v.id === state.selectedVideo.id);
      if (fresh) {
        const previous = state.selectedVideo;
        const statusChanged = fresh.status !== previous.status;
        const votJustFinished = fresh.job?.kind === 'vot_subtitles' && fresh.job?.status === 'done' && previous.job?.status !== 'done';
        state.selectedVideo = fresh;
        if (statusChanged && fresh.status === 'ready') await selectVideo(fresh.id);
        else if (votJustFinished) {
          setVideoMeta(fresh);
          await loadPhrases();
          await loadFlashcards();
        }
      }
    }
    const hasPending = state.videos.some(v => ['queued','processing'].includes(v.status) || ['queued','running'].includes(v.job?.status));
    if (hasPending && !state.pollTimer) state.pollTimer = setInterval(loadVideos, 2500);
    if (!hasPending && state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  } catch (e) {
    console.error(e);
  }
}

function statusText(v) {
  if (['queued','running'].includes(v.job?.status)) {
    const label = v.job?.kind === 'vot_subtitles' ? 'VOT' : 'JOB';
    return `${label} ${v.job?.progress ?? 0}%`;
  }
  if (v.status === 'ready') return 'READY';
  if (v.status === 'failed') return 'FAILED';
  return `${v.status.toUpperCase()} ${v.job?.progress ?? 0}%`;
}

function createLibraryItem(v) {
  const el = document.createElement('div');
  el.className = `library-item ${state.selectedVideo?.id === v.id ? 'active' : ''}`;
  el.innerHTML = `
    <div class="library-item-title"></div>
    <div class="library-item-meta">
      <span>${v.source_lang.toUpperCase()} → ${v.target_lang.toUpperCase()}</span>
      <span class="status-${v.status}">${statusText(v)}</span>
    </div>
    ${['queued','processing'].includes(v.status) ? `<div class="progress"><span style="width:${v.job?.progress ?? 3}%"></span></div>` : ''}
    ${v.status === 'failed' ? `<div class="error-text">${escapeHtml(v.error_message || v.job?.message || 'Ошибка')}</div>` : ''}
  `;
  el.querySelector('.library-item-title').textContent = v.title;
  el.addEventListener('click', async () => {
    closeDialog('libraryDialog');
    await selectVideo(v.id);
  });
  return el;
}

function renderLibrary() {
  for (const id of ['libraryList', 'mobileLibraryList']) {
    const root = $(id);
    if (!root) continue;
    root.innerHTML = '';
    if (!state.videos.length) {
      root.innerHTML = '<div class="subtle">Пока пусто</div>';
      continue;
    }
    for (const v of state.videos) root.appendChild(createLibraryItem(v));
  }
}

async function selectVideo(id) {
  const v = state.videos.find(x => x.id === id) || await api(`/api/videos/${id}`);
  state.selectedVideo = v;
  state.currentPhraseIndex = -1;
  state.dictionaryPhraseId = null;
  state.stats = {lookups: 0, phraseJumps: 0, loops: 0};
  state.seenPhraseIds = new Set();
  state.looping = false;
  state.mode = 'watch';
  updateLoopUI();
  updateModeUI();
  renderLibrary();

  $('emptyState').classList.add('hidden');
  $('playerView').classList.remove('hidden');
  $('videoTitle').textContent = v.title;
  setVideoMeta(v);
  $('menuVideoTitle').textContent = v.title;

  if (v.status === 'ready') {
    const desiredSrc = new URL(v.stream_url, location.href).href;
    if (player.src !== desiredSrc) {
      player.src = v.stream_url;
      player.load();
    }
    await Promise.all([loadPhrases(), loadFlashcards()]);
    syncPhraseToTime(player.currentTime || 0, true);
  } else {
    player.removeAttribute('src');
    player.load();
    state.phrases = [];
    state.flashcards = [];
    renderPhrases();
    renderSavedWords();
    renderStudyCard();
  }
}

async function loadPhrases() {
  if (!state.selectedVideo) return;
  const anchorTime = Number(player.currentTime || 0);
  state.phrases = await api(`/api/videos/${state.selectedVideo.id}/phrases`);

  // VOT sync can replace the whole phrase timeline. Never keep a stale array
  // index after that: derive the active card again from the actual video time.
  if (state.mode === 'watch') {
    state.currentPhraseIndex = findPhraseIndexAtTime(anchorTime);
  } else {
    state.currentPhraseIndex = findNearestPhraseIndex(anchorTime);
  }
  state.dictionaryPhraseId = state.currentPhraseIndex >= 0
    ? state.phrases[state.currentPhraseIndex]?.id ?? null
    : null;

  renderPhrases();
  renderStudyCard();
  if (state.mode === 'watch') {
    if (state.currentPhraseIndex >= 0) showOverlay(state.phrases[state.currentPhraseIndex]);
    else clearOverlay();
  }
}

async function loadFlashcards() {
  if (!state.selectedVideo) return;
  state.flashcards = await api(`/api/videos/${state.selectedVideo.id}/flashcards`);
  renderSavedWords();
}

function wordSegments(text, lang) {
  try {
    if ('Segmenter' in Intl) {
      return [...new Intl.Segmenter(lang || undefined, {granularity:'word'}).segment(text)]
        .map(x => ({text:x.segment, word:x.isWordLike}));
    }
  } catch (_) {}
  return String(text || '').split(/(\s+|[^\p{L}\p{N}'’-]+)/u).filter(Boolean)
    .map(x => ({text:x, word:/[\p{L}\p{N}]/u.test(x)}));
}

function renderClickableText(root, text, phrase) {
  root.innerHTML = '';
  for (const part of wordSegments(text, state.selectedVideo?.source_lang)) {
    if (!part.word) {
      root.appendChild(document.createTextNode(part.text));
      continue;
    }
    const span = document.createElement('span');
    span.className = 'word-token';
    span.textContent = part.text;
    span.title = 'Открыть словарь';
    span.addEventListener('click', ev => {
      ev.stopPropagation();
      startWordLookup(part.text, phrase);
    });
    root.appendChild(span);
  }
}

function renderPhrases() {
  const root = $('phraseList');
  root.innerHTML = '';
  if (!state.phrases.length) {
    root.innerHTML = '<div class="muted-box">Субтитры не найдены автоматически. Через меню можно добавить фразу вручную.</div>';
    clearOverlay();
    return;
  }

  state.phrases.forEach((p, i) => {
    const el = document.createElement('div');
    el.className = `phrase-row ${i === state.currentPhraseIndex ? 'active' : ''}`;
    el.dataset.phraseId = p.id;
    el.innerHTML = `
      <div class="phrase-time">${fmtTime(p.start_time)}</div>
      <div>
        <div class="phrase-source"></div>
        <div class="phrase-translation"></div>
      </div>
      <div class="phrase-actions">
        <button class="edit-phrase" title="Изменить">✎</button>
        <button class="delete-phrase" title="Удалить">×</button>
      </div>
    `;
    renderClickableText(el.querySelector('.phrase-source'), p.source_text, p);
    el.querySelector('.phrase-translation').textContent = p.translated_text || 'Перевод пока пуст';
    el.addEventListener('click', ev => {
      if (ev.target.closest('.phrase-actions') || ev.target.classList.contains('word-token')) return;
      closeDialog('transcriptDialog');
      goToStudyPhrase(i, true);
    });
    el.querySelector('.edit-phrase').addEventListener('click', ev => {
      ev.stopPropagation();
      closeDialog('transcriptDialog');
      openPhraseEditor(p);
    });
    el.querySelector('.delete-phrase').addEventListener('click', async ev => {
      ev.stopPropagation();
      if (!confirm('Удалить фразу?')) return;
      await api(`/api/phrases/${p.id}`, {method:'DELETE'});
      if (state.currentPhraseIndex === i) state.currentPhraseIndex = -1;
      await Promise.all([loadPhrases(), loadFlashcards()]);
    });
    root.appendChild(el);
  });
}

function findPhraseIndexAtTime(time) {
  let lo = 0;
  let hi = state.phrases.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const p = state.phrases[mid];
    if (time < p.start_time) hi = mid - 1;
    else if (time >= p.end_time) lo = mid + 1;
    else return mid;
  }
  return -1;
}

function findNearestPhraseIndex(time) {
  if (!state.phrases.length) return -1;
  const exact = findPhraseIndexAtTime(time);
  if (exact >= 0) return exact;
  for (let i = 0; i < state.phrases.length; i++) {
    if (state.phrases[i].start_time > time) return i;
  }
  return state.phrases.length - 1;
}

function showOverlay(p) {
  if (state.mode !== 'watch' || !p) {
    clearOverlay();
    return;
  }
  $('phraseOverlay').classList.remove('hidden');
  renderClickableText($('overlaySource'), p.source_text, p);
  $('overlayTranslation').textContent = p.translated_text || '';
  $('overlayTranslation').classList.toggle('hidden', !state.showTranslation || !p.translated_text);
}

function clearOverlay() {
  $('phraseOverlay').classList.add('hidden');
}

function syncPhraseToTime(time, force = false) {
  if (state.mode !== 'watch' || state.looping) return;
  const found = findPhraseIndexAtTime(time);
  if (!force && found === state.currentPhraseIndex) return;
  state.currentPhraseIndex = found;
  if (found >= 0) {
    const p = state.phrases[found];
    state.dictionaryPhraseId = p.id;
    state.seenPhraseIds.add(p.id);
    showOverlay(p);
  } else {
    clearOverlay();
  }
  renderPhrases();
}

function renderStudyCard() {
  const p = state.currentPhraseIndex >= 0 ? state.phrases[state.currentPhraseIndex] : null;
  const source = $('studySource');
  const target = $('studyTranslation');
  if (!p) {
    source.textContent = state.phrases.length ? 'Выбери фразу или нажми слово в субтитрах.' : 'Для этого видео пока нет фраз.';
    target.textContent = '';
    $('studyCounter').textContent = `0/${state.phrases.length}`;
    return;
  }
  renderClickableText(source, p.source_text, p);
  const translationText = p.translated_text || 'Перевод пока пуст';
  target.textContent = translationText;
  target.classList.toggle('compact', translationText.length > 150);
  target.classList.toggle('dense', translationText.length > 260);
  $('studyCounter').textContent = `${state.currentPhraseIndex + 1}/${state.phrases.length}`;
  state.dictionaryPhraseId = p.id;
  state.seenPhraseIds.add(p.id);
}

function updateModeUI() {
  const watch = state.mode === 'watch';
  $('studyPane').classList.toggle('hidden', watch);
  for (const id of ['modeWatchMobile','modeWatchDesktop','menuWatch']) $(id)?.classList.toggle('active', watch);
  for (const id of ['modeStudyMobile','modeStudyDesktop','menuStudy']) $(id)?.classList.toggle('active', !watch);
  if (watch) {
    const i = findPhraseIndexAtTime(player.currentTime || 0);
    state.currentPhraseIndex = i;
    if (i >= 0) showOverlay(state.phrases[i]); else clearOverlay();
  } else {
    clearOverlay();
    if (state.currentPhraseIndex < 0) state.currentPhraseIndex = findNearestPhraseIndex(player.currentTime || 0);
    renderStudyCard();
  }
  renderPhrases();
}

function setMode(mode, options = {}) {
  if (!['watch','study'].includes(mode)) return;
  state.mode = mode;
  if (mode === 'study') {
    if (options.pause !== false) player.pause();
    if (state.currentPhraseIndex < 0) state.currentPhraseIndex = findNearestPhraseIndex(player.currentTime || 0);
  }
  updateModeUI();
}

function goToStudyPhrase(index, autoplay = true) {
  if (!state.phrases.length) return;
  index = Math.max(0, Math.min(state.phrases.length - 1, index));
  const p = state.phrases[index];
  state.currentPhraseIndex = index;
  state.dictionaryPhraseId = p.id;
  state.stats.phraseJumps += 1;
  state.seenPhraseIds.add(p.id);
  state.mode = 'study';
  player.currentTime = p.start_time;
  updateModeUI();
  renderStudyCard();
  if (autoplay) player.play().catch(() => {});
}

function replayCurrentPhrase() {
  if (state.currentPhraseIndex < 0) return;
  const p = state.phrases[state.currentPhraseIndex];
  player.currentTime = p.start_time;
  player.play().catch(() => {});
}

function updateLoopUI() {
  $('studyLoop').textContent = `Loop ${state.looping ? 'ON' : 'OFF'}`;
  $('menuLoop').querySelector('strong').textContent = state.looping ? 'ON' : 'OFF';
  $('menuLoop').classList.toggle('active', state.looping);
}

function toggleLoop() {
  if (!state.looping && state.currentPhraseIndex < 0) state.currentPhraseIndex = findNearestPhraseIndex(player.currentTime || 0);
  state.looping = !state.looping;
  updateLoopUI();
  if (state.looping && state.currentPhraseIndex >= 0) replayCurrentPhrase();
  if (!state.looping && state.mode === 'watch') syncPhraseToTime(player.currentTime, true);
}

function updateSpeedUI() {
  const label = `${Number(player.playbackRate).toFixed(2).replace(/\.00$/,'').replace(/0$/,'')}×`;
  $('studySpeed').textContent = label;
  $('menuSpeed').querySelector('strong').textContent = label;
}

function cycleSpeed() {
  const current = Number(player.playbackRate || 1);
  let idx = SPEEDS.findIndex(x => Math.abs(x - current) < 0.01);
  idx = (idx + 1) % SPEEDS.length;
  player.playbackRate = SPEEDS[idx];
  updateSpeedUI();
}

function updateTranslationUI() {
  $('menuTranslation').querySelector('strong').textContent = state.showTranslation ? 'ON' : 'OFF';
  if (state.mode === 'watch' && state.currentPhraseIndex >= 0) showOverlay(state.phrases[state.currentPhraseIndex]);
}

function frameLoop(_now, metadata) {
  const time = metadata.mediaTime;
  if (state.looping && state.currentPhraseIndex >= 0) {
    const p = state.phrases[state.currentPhraseIndex];
    if (p && time >= p.end_time) {
      state.stats.loops += 1;
      player.currentTime = p.start_time;
      player.play().catch(() => {});
    }
  } else if (state.mode === 'watch') {
    syncPhraseToTime(time);
  } else if (state.mode === 'study' && state.currentPhraseIndex >= 0 && !player.paused) {
    const p = state.phrases[state.currentPhraseIndex];
    if (p && time >= p.end_time) {
      player.pause();
      player.currentTime = Math.max(p.start_time, p.end_time - 0.03);
    }
  }
  if ('requestVideoFrameCallback' in HTMLVideoElement.prototype) player.requestVideoFrameCallback(frameLoop);
}

if ('requestVideoFrameCallback' in HTMLVideoElement.prototype) {
  player.requestVideoFrameCallback(frameLoop);
} else {
  player.addEventListener('timeupdate', () => {
    const time = player.currentTime;
    if (state.looping && state.currentPhraseIndex >= 0) {
      const p = state.phrases[state.currentPhraseIndex];
      if (p && time >= p.end_time) {
        state.stats.loops += 1;
        player.currentTime = p.start_time;
        player.play().catch(() => {});
      }
    } else if (state.mode === 'watch') {
      syncPhraseToTime(time);
    } else if (state.mode === 'study' && state.currentPhraseIndex >= 0 && !player.paused) {
      const p = state.phrases[state.currentPhraseIndex];
      if (p && time >= p.end_time) player.pause();
    }
  });
}

player.addEventListener('seeked', () => {
  if (state.mode === 'watch') syncPhraseToTime(player.currentTime, true);
  else {
    const i = findNearestPhraseIndex(player.currentTime);
    if (i >= 0) {
      state.currentPhraseIndex = i;
      renderStudyCard();
      renderPhrases();
    }
  }
});
player.addEventListener('ratechange', updateSpeedUI);

for (const id of ['modeWatchMobile','modeWatchDesktop','menuWatch']) $(id)?.addEventListener('click', () => {
  setMode('watch', {pause:false});
  closeDialog('menuDialog');
});
for (const id of ['modeStudyMobile','modeStudyDesktop','menuStudy']) $(id)?.addEventListener('click', () => {
  setMode('study');
  closeDialog('menuDialog');
});

$('studyPrev').addEventListener('click', () => goToStudyPhrase((state.currentPhraseIndex < 0 ? 0 : state.currentPhraseIndex - 1), true));
$('studyNext').addEventListener('click', () => goToStudyPhrase((state.currentPhraseIndex < 0 ? 0 : state.currentPhraseIndex + 1), true));
$('studyReplay').addEventListener('click', replayCurrentPhrase);
$('studyLoop').addEventListener('click', toggleLoop);
$('studySpeed').addEventListener('click', cycleSpeed);
$('studyEdit').addEventListener('click', () => {
  const p = state.phrases[state.currentPhraseIndex];
  if (p) openPhraseEditor(p);
});
$('studySaveSentence').addEventListener('click', () => $('savedDialog').showModal());

const studyCard = $('studyCard');
studyCard.addEventListener('pointerdown', ev => {
  if (ev.target.closest('button') || ev.target.classList.contains('word-token')) return;
  state.swipeStart = {x:ev.clientX, y:ev.clientY, t:Date.now()};
});
studyCard.addEventListener('pointerup', ev => {
  if (!state.swipeStart) return;
  const dx = ev.clientX - state.swipeStart.x;
  const dy = ev.clientY - state.swipeStart.y;
  const dt = Date.now() - state.swipeStart.t;
  state.swipeStart = null;
  if (dt > 900 || Math.abs(dx) < 45 || Math.abs(dx) < Math.abs(dy) * 1.15) return;
  if (dx < 0) goToStudyPhrase((state.currentPhraseIndex < 0 ? 0 : state.currentPhraseIndex + 1), true);
  else goToStudyPhrase((state.currentPhraseIndex <= 0 ? 0 : state.currentPhraseIndex - 1), true);
});

async function refreshVotStatus() {
  const status = $('menuVotStatus');
  if (!status || !state.selectedVideo) return;
  if (!state.selectedVideo.source_url) {
    status.textContent = 'VOT: нужен исходный URL видео';
    return;
  }
  status.textContent = 'VOT: проверяю vot.js…';
  try {
    const data = await api('/api/vot/status');
    if (data.available) {
      status.textContent = `vot.js ${data.vot_js_version || '?'} · direct bridge`;
    } else {
      status.textContent = `VOT недоступен${data.error ? ': ' + data.error : ''}`;
    }
  } catch (e) {
    status.textContent = `VOT недоступен: ${e.message}`;
  }
}

function openMenu() {
  if (!state.selectedVideo) return;
  $('menuVideoTitle').textContent = state.selectedVideo.title;
  updateModeUI();
  updateLoopUI();
  updateSpeedUI();
  updateTranslationUI();
  $('menuDialog').showModal();
  refreshVotStatus();
}
$('menuOpen').addEventListener('click', openMenu);
$('menuSpeed').addEventListener('click', cycleSpeed);
$('menuLoop').addEventListener('click', toggleLoop);
$('menuTranslation').addEventListener('click', () => {
  state.showTranslation = !state.showTranslation;
  updateTranslationUI();
});
$('menuTranscript').addEventListener('click', () => {
  closeDialog('menuDialog');
  renderPhrases();
  $('transcriptDialog').showModal();
});
$('menuLibrary').addEventListener('click', () => {
  closeDialog('menuDialog');
  $('libraryDialog').showModal();
});
$('menuSaved').addEventListener('click', () => {
  closeDialog('menuDialog');
  $('savedDialog').showModal();
});
$('menuVot').addEventListener('click', () => {
  if (!state.selectedVideo) return;
  const status = $('menuVotStatus');
  if (!state.selectedVideo.source_url) {
    status.textContent = 'Для локально загруженного файла VOT недоступен';
    return;
  }

  closeDialog('menuDialog');
  $('subtitleSourceLang').value = 'auto';
  $('subtitleTargetLang').value = state.selectedVideo.target_lang || 'ru';
  $('subtitleCurrentLangs').textContent = `Сейчас в уроке: ${String(state.selectedVideo.source_lang || '?').toUpperCase()} → ${String(state.selectedVideo.target_lang || '?').toUpperCase()} · ${state.phrases.length} фраз`;
  $('subtitleRegenerateStatus').textContent = '';
  $('subtitleRegenerate').disabled = false;
  $('subtitleRegenerate').textContent = '↻ Перегенерировать субтитры';
  $('subtitleDialog').showModal();
});

$('subtitleDialogClose').addEventListener('click', () => closeDialog('subtitleDialog'));

$('subtitleRegenerate').addEventListener('click', async () => {
  if (!state.selectedVideo) return;
  const button = $('subtitleRegenerate');
  const status = $('subtitleRegenerateStatus');
  const source = normalizeLangCode($('subtitleSourceLang').value || 'auto', 'auto');
  const target = normalizeLangCode($('subtitleTargetLang').value || 'ru', 'ru');

  if (!source || !target) {
    status.textContent = 'Укажи source и target language.';
    return;
  }
  if (source !== 'auto' && source === target) {
    status.textContent = 'Source и Target должны отличаться.';
    return;
  }

  button.disabled = true;
  button.textContent = 'Ставлю в очередь…';
  status.textContent = source === 'auto'
    ? `VOT сам определит язык речи → ${target.toUpperCase()}`
    : `${source.toUpperCase()} → ${target.toUpperCase()}`;

  try {
    const job = await api(`/api/videos/${state.selectedVideo.id}/vot-subtitles`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source_lang:source, target_lang:target}),
    });
    status.textContent = `VOT: ${job.message || 'в очереди'}`;
    button.textContent = '✓ Запущено';
    setTimeout(() => closeDialog('subtitleDialog'), 450);
    if (!state.pollTimer) state.pollTimer = setInterval(loadVideos, 2500);
  } catch (e) {
    status.textContent = e.message;
    button.disabled = false;
    button.textContent = '↻ Перегенерировать субтитры';
  }
});

$('menuDictionaries').addEventListener('click', async () => {
  closeDialog('menuDialog');
  await openDictionaryManager();
});
$('menuFinish').addEventListener('click', () => {
  closeDialog('menuDialog');
  showLessonSummary();
});

$('menuDeleteLesson').addEventListener('click', () => {
  if (!state.selectedVideo) return;
  const v = state.selectedVideo;
  if (['queued','processing'].includes(v.status) || ['queued','running'].includes(v.job?.status)) {
    alert('Этот урок сейчас обрабатывается. Дождись окончания текущей задачи и затем удали его.');
    return;
  }
  closeDialog('menuDialog');
  $('deleteLessonTitle').textContent = v.title;
  const bits = [];
  if (v.file_size_bytes) bits.push(fmtBytes(v.file_size_bytes));
  bits.push(`${state.phrases.length} фраз`);
  bits.push(`${state.flashcards.length} сохранённых слов`);
  $('deleteLessonMeta').textContent = bits.join(' · ');
  $('deleteLessonConfirm').disabled = false;
  $('deleteLessonConfirm').textContent = 'Удалить урок';
  $('deleteLessonDialog').showModal();
});

$('deleteLessonClose').addEventListener('click', () => closeDialog('deleteLessonDialog'));
$('deleteLessonCancel').addEventListener('click', () => closeDialog('deleteLessonDialog'));
$('deleteLessonConfirm').addEventListener('click', async () => {
  const v = state.selectedVideo;
  if (!v) return closeDialog('deleteLessonDialog');
  const button = $('deleteLessonConfirm');
  button.disabled = true;
  button.textContent = 'Удаляю…';
  try {
    const deletedId = v.id;
    await api(`/api/videos/${deletedId}`, {method:'DELETE'});
    closeDialog('deleteLessonDialog');
    clearSelectedLesson();
    await loadVideos();
    if (state.videos.length) await selectVideo(state.videos[0].id);
  } catch (e) {
    button.disabled = false;
    button.textContent = 'Удалить урок';
    alert(`Не удалось удалить урок: ${e.message}`);
  }
});
$('menuAddVideo').addEventListener('click', () => {
  closeDialog('menuDialog');
  openAdd();
});
$('menuEditPhrase').addEventListener('click', () => {
  closeDialog('menuDialog');
  const p = state.phrases[state.currentPhraseIndex];
  if (p) openPhraseEditor(p); else alert('Сейчас нет активной фразы.');
});
$('menuAddPhrase').addEventListener('click', () => {
  closeDialog('menuDialog');
  openNewPhrase();
});

$('mobileLibraryOpen').addEventListener('click', () => $('libraryDialog').showModal());
$('mobileAddVideo').addEventListener('click', () => {
  closeDialog('libraryDialog');
  openAdd();
});
$('savedBadge').addEventListener('click', () => $('savedDialog').showModal());
$('savedClose').addEventListener('click', () => closeDialog('savedDialog'));

async function openDictionaryManager() {
  if (state.selectedVideo) {
    $('dictSourceLang').value = state.selectedVideo.source_lang || 'en';
    $('dictTargetLang').value = state.selectedVideo.target_lang || 'ru';
  }
  $('dictionaryUploadStatus').textContent = '';
  await loadDictionaryManager();
  $('dictionaryManagerDialog').showModal();
}

async function loadDictionaryManager() {
  const root = $('dictionaryManagerList');
  root.innerHTML = '<div class="subtle">Загрузка…</div>';
  try {
    const items = await api('/api/dictionaries');
    root.innerHTML = '';
    if (!items.length) {
      root.innerHTML = '<div class="muted-box">Словарей пока нет. Загрузи StarDict ZIP или его файлы.</div>';
      return;
    }
    for (const d of items) {
      const row = document.createElement('div');
      row.className = 'dictionary-manager-row';
      row.innerHTML = `
        <div>
          <strong>${escapeHtml(d.name)}</strong>
          <small>${escapeHtml(String(d.source_lang).toUpperCase())} → ${escapeHtml(String(d.target_lang).toUpperCase())} · StarDict</small>
        </div>
        <button class="icon-btn dict-delete" title="Удалить">×</button>
      `;
      row.querySelector('.dict-delete').addEventListener('click', async () => {
        if (!confirm(`Удалить словарь «${d.name}» с сервера?`)) return;
        await api(`/api/dictionaries/${d.id}`, {method:'DELETE'});
        await loadDictionaryManager();
      });
      root.appendChild(row);
    }
  } catch (e) {
    root.innerHTML = `<div class="error-text">${escapeHtml(e.message)}</div>`;
  }
}

$('dictionaryManagerClose').addEventListener('click', () => closeDialog('dictionaryManagerDialog'));
$('dictionaryUpload').addEventListener('click', async () => {
  const files = [...$('dictionaryFiles').files];
  const status = $('dictionaryUploadStatus');
  if (!files.length) { status.textContent = 'Сначала выбери ZIP или файлы словаря.'; return; }
  const fd = new FormData();
  for (const file of files) fd.append('files', file);
  fd.append('source_lang', normalizeLangCode($('dictSourceLang').value, 'en'));
  fd.append('target_lang', normalizeLangCode($('dictTargetLang').value, 'ru'));
  $('dictionaryUpload').disabled = true;
  status.textContent = `Загружаю ${files.length} файл(а)…`;
  try {
    const data = await api('/api/dictionaries/upload', {method:'POST', body:fd});
    status.textContent = `Готово: ${data.added.map(x => x.name).join(', ')}`;
    $('dictionaryFiles').value = '';
    await loadDictionaryManager();
  } catch (e) {
    status.textContent = e.message;
  } finally {
    $('dictionaryUpload').disabled = false;
  }
});
$('dictionaryRescan').addEventListener('click', async () => {
  const status = $('dictionaryUploadStatus');
  try {
    const data = await api(`/api/dictionaries/scan?source_lang=${encodeURIComponent(normalizeLangCode($('dictSourceLang').value, 'en'))}&target_lang=${encodeURIComponent(normalizeLangCode($('dictTargetLang').value, 'ru'))}`, {method:'POST'});
    status.textContent = `Сканирование: найдено новых ${data.added.length}`;
    await loadDictionaryManager();
  } catch (e) { status.textContent = e.message; }
});

function openAdd() {
  $('addDialog').showModal();
}
$('addOpen').addEventListener('click', openAdd);
$('emptyAdd').addEventListener('click', openAdd);
$('refreshBtn').addEventListener('click', loadVideos);

function setAddMode(mode) {
  state.addMode = mode;
  $('tabUrl').classList.toggle('active', mode === 'url');
  $('tabFile').classList.toggle('active', mode === 'file');
  $('urlPanel').classList.toggle('hidden', mode !== 'url');
  $('filePanel').classList.toggle('hidden', mode !== 'file');
}
$('tabUrl').addEventListener('click', () => setAddMode('url'));
$('tabFile').addEventListener('click', () => setAddMode('file'));

$('submitAdd').addEventListener('click', async () => {
  const btn = $('submitAdd');
  const status = $('addStatus');
  btn.disabled = true;
  status.textContent = 'Отправляю…';
  try {
    let created;
    const sourceLang = normalizeLangCode($('sourceLang').value, 'en');
    const targetLang = normalizeLangCode($('targetLang').value, 'ru');
    const title = $('videoTitleInput').value.trim();
    if (state.addMode === 'url') {
      const url = $('videoUrl').value.trim();
      if (!url) throw new Error('Вставь URL');
      created = await api('/api/videos/url', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url, title:title || null, source_lang:sourceLang, target_lang:targetLang})
      });
    } else {
      const file = $('videoFile').files[0];
      if (!file) throw new Error('Выбери видеофайл');
      const fd = new FormData();
      fd.append('file', file);
      fd.append('title', title || file.name);
      fd.append('source_lang', sourceLang);
      fd.append('target_lang', targetLang);
      created = await api('/api/videos/upload', {method:'POST', body:fd});
    }
    $('addDialog').close();
    $('videoUrl').value = '';
    $('videoFile').value = '';
    status.textContent = '';
    await loadVideos();
    await selectVideo(created.id);
  } catch (e) {
    status.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

function openNewPhrase() {
  if (!state.selectedVideo || state.selectedVideo.status !== 'ready') return;
  $('phraseEditId').value = '';
  $('phraseStart').value = Math.max(0, player.currentTime).toFixed(2);
  $('phraseEnd').value = Math.max(0, player.currentTime + 3).toFixed(2);
  $('phraseSource').value = '';
  $('phraseTranslation').value = '';
  $('phraseDialog').showModal();
}

function openPhraseEditor(p) {
  $('phraseEditId').value = p.id;
  $('phraseStart').value = Number(p.start_time).toFixed(2);
  $('phraseEnd').value = Number(p.end_time).toFixed(2);
  $('phraseSource').value = p.source_text;
  $('phraseTranslation').value = p.translated_text || '';
  $('phraseDialog').showModal();
}

$('useNowStart').addEventListener('click', () => $('phraseStart').value = player.currentTime.toFixed(2));
$('useNowEnd').addEventListener('click', () => $('phraseEnd').value = player.currentTime.toFixed(2));
$('savePhrase').addEventListener('click', async () => {
  try {
    if (!state.selectedVideo) return;
    const payload = {
      start_time:Number($('phraseStart').value),
      end_time:Number($('phraseEnd').value),
      source_text:$('phraseSource').value.trim(),
      translated_text:$('phraseTranslation').value.trim(),
    };
    const editId = $('phraseEditId').value;
    if (editId) {
      await api(`/api/phrases/${editId}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    } else {
      await api(`/api/videos/${state.selectedVideo.id}/phrases`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    }
    $('phraseDialog').close();
    await loadPhrases();
    if (state.mode === 'study') renderStudyCard();
  } catch (e) {
    alert(e.message);
  }
});

function phraseForId(id) {
  return state.phrases.find(p => p.id === id) || null;
}

function startWordLookup(word, phrase) {
  closeDialog('transcriptDialog');
  const cleanWord = String(word || '').replace(/^\P{L}+|\P{L}+$/gu, '');
  if (!cleanWord) return;

  if (phrase) {
    const idx = state.phrases.findIndex(p => p.id === phrase.id);
    if (idx >= 0) state.currentPhraseIndex = idx;
    state.dictionaryPhraseId = phrase.id;
  } else if (state.currentPhraseIndex >= 0) {
    state.dictionaryPhraseId = state.phrases[state.currentPhraseIndex]?.id ?? null;
  }

  // A tap on a movie subtitle means: pause movie, enter study mode, inspect word.
  player.pause();
  setMode('study', {pause:true});
  renderStudyCard();

  const context = phrase || phraseForId(state.dictionaryPhraseId);
  $('dictionaryTitle').textContent = cleanWord;
  $('dictWord').value = cleanWord;
  $('dictionaryPhraseSource').textContent = context?.source_text || '';
  $('dictionaryPhraseTarget').textContent = context?.translated_text || '';
  if (!$('dictionaryDialog').open) $('dictionaryDialog').showModal();
  lookupWord();
}

$('dictClose').addEventListener('click', () => closeDialog('dictionaryDialog'));
$('dictLookup').addEventListener('click', lookupWord);
$('dictWord').addEventListener('keydown', e => {
  if (e.key === 'Enter') lookupWord();
});

function suggestedTranslation(definition) {
  const clean = String(definition || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  if (!clean) return '';
  const first = clean.split(/\s*[;；|]\s*|\s{2,}/)[0].trim();
  return first.slice(0, 240);
}

async function lookupWord() {
  if (!state.selectedVideo) return;
  const word = $('dictWord').value.trim();
  if (!word) return;
  if (!state.dictionaryPhraseId && state.currentPhraseIndex >= 0) state.dictionaryPhraseId = state.phrases[state.currentPhraseIndex]?.id;
  state.stats.lookups += 1;
  $('dictionaryTitle').textContent = word;
  $('dictResult').textContent = 'Ищу…';

  try {
    const data = await api(`/api/dictionaries/lookup?word=${encodeURIComponent(word)}&source_lang=${encodeURIComponent(state.selectedVideo.source_lang)}&target_lang=${encodeURIComponent(state.selectedVideo.target_lang)}`);
    if (!data.results.length) {
      $('dictResult').textContent = 'Совпадений нет. Открой ⋮ → Словари и загрузи StarDict с телефона или ПК.';
      return;
    }

    $('dictResult').innerHTML = '';
    for (const r of data.results) {
      const box = document.createElement('div');
      box.className = 'dict-entry';
      box.innerHTML = `
        <div class="dict-name">${escapeHtml(r.dictionary)}</div>
        <div class="dict-definition">${escapeHtml(r.definition)}</div>
        <label>Перевод для Anki</label>
        <input class="dict-target" value="${escapeHtml(suggestedTranslation(r.definition))}" />
        <button class="btn primary full save-word">＋ Сохранить слово</button>
      `;
      box.querySelector('.save-word').addEventListener('click', async () => {
        const phraseId = state.dictionaryPhraseId;
        const target = box.querySelector('.dict-target').value.trim();
        if (!phraseId) return alert('Слово должно быть связано с конкретной фразой.');
        if (!target) return alert('Укажи перевод слова.');
        try {
          await api(`/api/phrases/${phraseId}/flashcards`, {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({source_word:word, target_word:target, dictionary_name:r.dictionary}),
          });
          await loadFlashcards();
          box.querySelector('.save-word').textContent = '✓ Сохранено';
        } catch (e) {
          alert(e.message);
        }
      });
      $('dictResult').appendChild(box);
    }
  } catch (e) {
    $('dictResult').textContent = e.message;
  }
}

function renderSavedWords() {
  document.querySelectorAll('.saved-count').forEach(el => el.textContent = state.flashcards.length);
  const root = $('savedWords');
  root.innerHTML = '';
  if (!state.flashcards.length) {
    root.innerHTML = '<div class="subtle">Пока пусто. Нажимай незнакомые слова в субтитрах или карточке.</div>';
    return;
  }

  for (const c of state.flashcards) {
    const row = document.createElement('div');
    row.className = 'saved-word-row';
    row.innerHTML = `
      <div><strong>${escapeHtml(c.source_word)}</strong><span> → ${escapeHtml(c.target_word)}</span></div>
      <button class="remove-saved" title="Удалить">×</button>
    `;
    row.querySelector('.remove-saved').addEventListener('click', async () => {
      await api(`/api/flashcards/${c.id}`, {method:'DELETE'});
      await loadFlashcards();
    });
    root.appendChild(row);
  }
}

async function showLessonSummary() {
  if (!state.selectedVideo) return;
  await loadFlashcards();
  $('summaryTitle').textContent = state.selectedVideo.title;
  $('statWords').textContent = state.flashcards.length;
  $('statPhrases').textContent = `${state.seenPhraseIds.size} / ${state.phrases.length}`;
  $('statLookups').textContent = state.stats.lookups;
  $('statLoops').textContent = state.stats.loops;
  $('summaryWords').innerHTML = state.flashcards.length
    ? state.flashcards.map(c => `<span class="summary-chip">${escapeHtml(c.source_word)} → ${escapeHtml(c.target_word)}</span>`).join('')
    : '<div class="subtle">В этом уроке ты пока не сохранил слов.</div>';
  const exportBtn = $('exportAnki');
  exportBtn.disabled = state.flashcards.length === 0;
  exportBtn.textContent = state.flashcards.length ? `Экспортировать ${state.flashcards.length} в Anki` : 'Нет слов для Anki';
  $('lessonDialog').showModal();
}

$('finishLessonDesktop').addEventListener('click', showLessonSummary);
player.addEventListener('ended', () => {
  if (state.mode === 'watch') showLessonSummary();
});
$('exportAnki').addEventListener('click', () => {
  if (!state.selectedVideo || !state.flashcards.length) return;
  window.location.href = `/api/videos/${state.selectedVideo.id}/anki`;
});

// Close sheet dialogs when tapping the dark backdrop.
for (const id of ['menuDialog','subtitleDialog','libraryDialog','transcriptDialog','dictionaryDialog','savedDialog','dictionaryManagerDialog','deleteLessonDialog']) {
  const d = $(id);
  d?.addEventListener('click', ev => {
    if (ev.target === d) d.close();
  });
}

updateSpeedUI();
updateLoopUI();
updateTranslationUI();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/assets/sw.js?v=0.10').catch(() => {});
loadVideos();
