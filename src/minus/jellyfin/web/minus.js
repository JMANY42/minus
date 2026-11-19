/*
 * Minus search + download UI for Jellyfin.
 *
 * Served as a single /minus.js asset and loaded into the Jellyfin web client.
 * There is no build step and no bundler: everything (CSS included) lives in
 * this file so the bridge can serve one immutable response and expose the
 * per-process token in the same place.
 *
 * The launcher and modal are appended straight to <body>, outside #reactRoot.
 * Jellyfin re-renders its own tree on every route change, and anything mounted
 * inside it would be wiped out mid-search. The only exception is the inline
 * empty-search bar, which has to sit in the page content and is therefore
 * re-derived after each render by the observer below.
 */
(function () {
  'use strict';

  var config = window.__MINUS_JELLYFIN__;
  if (!config || !config.base) return;
  if (document.querySelector('[data-minus="fab"]')) return;

  var BASE = String(config.base).replace(/\/+$/, '');
  var TOKEN = String(config.token || '');

  var SEARCH_LIMIT = 8;
  var INLINE_DELAY = 700;
  var INLINE_SETTLE = 120;
  var POLL_MS = 2000;
  var JOB_CAP_MS = 30 * 60 * 1000;

  var PRIMARY = 'var(--theme-primary-color, #00a4dc)';

  var STYLE = `
    .minus-fab,
    .minus-overlay,
    .minus-overlay * {
      box-sizing: border-box;
    }
    .minus-fab {
      position: fixed !important;
      right: 20px;
      bottom: 20px;
      z-index: 2147483000;
      display: flex;
      align-items: center;
      justify-content: center;
      width: 52px;
      height: 52px;
      padding: 0;
      border: 0;
      border-radius: 50%;
      background: ${PRIMARY};
      color: #fff;
      cursor: pointer;
      box-shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
      transition: filter 120ms ease, transform 120ms ease;
    }
    .minus-fab:hover { filter: brightness(1.1); transform: translateY(-1px); }
    .minus-fab:focus-visible {
      outline: 3px solid #fff;
      outline-offset: 3px;
    }
    .minus-fab svg { width: 24px; height: 24px; display: block; }

    .minus-overlay {
      position: fixed !important;
      inset: 0;
      z-index: 2147483001;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      padding: clamp(12px, 6vh, 72px) 16px 24px;
      background: rgba(3, 5, 8, 0.66);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    .minus-overlay[hidden],
    .minus-modal [hidden],
    .minus-row [hidden],
    .minus-job [hidden] {
      display: none !important;
    }

    .minus-modal {
      display: flex;
      flex-direction: column;
      width: min(660px, 100%);
      max-height: min(84vh, 900px);
      overflow: auto;
      padding: 18px;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 14px;
      background: #151a20;
      color: #eef2f7;
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.6);
    }

    .minus-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .minus-title {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0.01em;
    }
    .minus-close {
      width: 34px;
      height: 34px;
      padding: 0;
      border: 0;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.08);
      color: #eef2f7;
      font-size: 22px;
      line-height: 1;
      cursor: pointer;
    }
    .minus-close:hover { background: rgba(255, 255, 255, 0.16); }
    .minus-close:focus-visible,
    .minus-btn:focus-visible,
    .minus-input:focus-visible {
      outline: 2px solid ${PRIMARY};
      outline-offset: 2px;
    }

    .minus-form {
      display: flex;
      gap: 8px;
      margin-bottom: 12px;
    }
    .minus-input {
      flex: 1 1 auto;
      min-width: 0;
      padding: 10px 12px;
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 9px;
      background: #0d1116;
      color: #f4f7fb;
      font: inherit;
      font-size: 15px;
    }
    .minus-input::placeholder { color: #8d99a8; }

    .minus-btn {
      padding: 9px 14px;
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 9px;
      background: rgba(255, 255, 255, 0.08);
      color: #f4f7fb;
      font: inherit;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
    }
    .minus-btn:hover { background: rgba(255, 255, 255, 0.15); }
    .minus-btn:disabled {
      opacity: 0.45;
      cursor: not-allowed;
      filter: none;
    }
    .minus-btn-primary {
      border-color: transparent;
      background: ${PRIMARY};
      color: #fff;
    }
    .minus-btn-primary:hover:not(:disabled) { filter: brightness(1.1); background: ${PRIMARY}; }

    .minus-status {
      min-height: 20px;
      margin-bottom: 10px;
      color: #a9b4c2;
      font-size: 13px;
      line-height: 1.4;
    }
    .minus-status[data-tone="error"] { color: #ff9a9a; }
    .minus-status[data-tone="ok"] { color: #8be3b0; }
    .minus-status[data-tone="empty"] { color: #d5c47a; }

    .minus-results { display: flex; flex-direction: column; gap: 10px; }

    .minus-warnings {
      padding: 9px 11px;
      border: 1px solid rgba(255, 196, 92, 0.3);
      border-radius: 9px;
      background: rgba(255, 196, 92, 0.08);
      color: #e8cf9a;
      font-size: 12px;
      line-height: 1.45;
    }
    .minus-warning + .minus-warning { margin-top: 3px; }

    .minus-row {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      padding: 12px;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 11px;
      background: #10151b;
    }
    .minus-row-body { min-width: 0; flex: 1 1 auto; }
    .minus-row-title {
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 3;
      line-clamp: 3;
      overflow: hidden;
      color: #f4f7fb;
      font-size: 14px;
      font-weight: 600;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .minus-row-meta {
      margin-top: 4px;
      color: #a9b4c2;
      font-size: 12.5px;
      line-height: 1.4;
    }
    .minus-row-reasons {
      margin-top: 3px;
      color: #7f8b9a;
      font-size: 11.5px;
      line-height: 1.4;
    }
    .minus-row-note {
      margin-top: 5px;
      color: #e8cf9a;
      font-size: 12px;
      line-height: 1.4;
    }
    .minus-row-note[data-tone="error"] { color: #ff9a9a; }
    .minus-row-actions {
      display: flex;
      flex: 0 0 auto;
      flex-direction: column;
      align-items: stretch;
      gap: 5px;
    }
    .minus-row-actions small {
      max-width: 150px;
      color: #8d99a8;
      font-size: 11px;
      line-height: 1.35;
      text-align: right;
    }

    .minus-job {
      margin-bottom: 12px;
      padding: 11px 12px;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 10px;
      background: #0d1218;
    }
    .minus-job-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .minus-job-title { font-size: 13px; font-weight: 650; }
    .minus-job-state {
      padding: 2px 8px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.1);
      color: #dbe4ef;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .minus-job[data-state="error"] .minus-job-state { background: rgba(255, 90, 90, 0.2); color: #ffb0b0; }
    .minus-job[data-state="done"] .minus-job-state { background: rgba(80, 210, 140, 0.2); color: #a9f0c8; }
    .minus-job-meta {
      margin-top: 4px;
      color: #7f8b9a;
      font-size: 11.5px;
      overflow-wrap: anywhere;
    }
    .minus-job-detail {
      margin-top: 5px;
      color: #cfd8e3;
      font-size: 12.5px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .minus-job-result { margin-top: 6px; }
    .minus-job-line {
      color: #a9b4c2;
      font-size: 12.5px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .minus-job-line[data-tone="ok"] { color: #8be3b0; }
    .minus-job-line[data-tone="error"] { color: #ff9a9a; }
    .minus-job-line[data-tone="warn"] { color: #e8cf9a; }

    .minus-inline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      max-width: 760px;
      margin: 18px auto;
      padding: 13px 15px;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 12px;
      background: #151a20;
      color: #eef2f7;
      box-shadow: 0 8px 30px rgba(0, 0, 0, 0.28);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      text-align: left;
    }
    .minus-inline-text { font-size: 14px; line-height: 1.4; }
    .minus-inline .minus-btn { flex: 0 0 auto; }
  `;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function formatScore(value) {
    var number = Number(value);
    if (!isFinite(number)) return '?';
    return String(Math.round(number * 10) / 10);
  }

  function humanSize(bytes) {
    var number = Number(bytes);
    if (!isFinite(number) || number <= 0) return '';
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    var index = 0;
    while (number >= 1024 && index < units.length - 1) {
      number /= 1024;
      index += 1;
    }
    var rounded = index === 0 || number >= 10 ? Math.round(number) : Math.round(number * 10) / 10;
    return rounded + ' ' + units[index];
  }

  function plural(count, singular) {
    return count === 1 ? singular : singular + 's';
  }

  function firstString(values) {
    for (var i = 0; i < values.length; i += 1) {
      var value = values[i];
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
    return '';
  }

  function safeJson(value) {
    try {
      var text = JSON.stringify(value);
      if (!text) return '';
      return text.length > 500 ? text.slice(0, 497) + '…' : text;
    } catch (error) {
      return '';
    }
  }

  function injectStyles() {
    var style = document.createElement('style');
    style.setAttribute('data-minus', 'style');
    style.textContent = STYLE;
    document.head.appendChild(style);
  }

  // ---------------------------------------------------------------- API

  function apiFetch(path, options) {
    options = options || {};
    var headers = options.headers || {};
    headers['X-Minus-Token'] = TOKEN;
    options.headers = headers;
    return fetch(BASE + path, options);
  }

  function serverError(response) {
    return response
      .json()
      .then(
        function (body) {
          if (body && typeof body.error === 'string' && body.error) return body.error;
          return 'Minus returned HTTP ' + response.status + '.';
        },
        function () {
          return 'Minus returned HTTP ' + response.status + '.';
        }
      );
  }

  function failed(response) {
    return serverError(response).then(function (message) {
      throw new Error(message);
    });
  }

  // ------------------------------------------------------------- launcher

  var overlay;
  var modal;
  var input;
  var resultsBox;
  var statusBox;
  var jobBox;
  var lastFocused = null;

  function buildLauncher() {
    var fab = el('button', 'minus-fab');
    fab.type = 'button';
    fab.setAttribute('data-minus', 'fab');
    fab.setAttribute('aria-label', 'Search and download with Minus');
    fab.setAttribute('title', 'Search and download with Minus');
    fab.innerHTML =
      '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
      '<path fill="currentColor" d="M9.5 3a6.5 6.5 0 1 0 4.03 11.6l4.43 4.43 1.41-1.41-4.43-4.43A6.5 6.5 0 0 0 9.5 3zm0 2a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9z"/>' +
      '</svg>';
    fab.addEventListener('click', function () {
      openModal('', false);
    });
    document.body.appendChild(fab);
  }

  function buildModal() {
    overlay = el('div', 'minus-overlay');
    overlay.setAttribute('data-minus', 'backdrop');
    overlay.hidden = true;

    modal = el('div', 'minus-modal');
    modal.setAttribute('data-minus', 'modal');
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-label', 'Search and download with Minus');

    var head = el('div', 'minus-head');
    head.appendChild(el('h2', 'minus-title', 'Minus'));
    var close = el('button', 'minus-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'Close Minus');
    close.addEventListener('click', closeModal);
    head.appendChild(close);
    modal.appendChild(head);

    var form = el('form', 'minus-form');
    form.setAttribute('role', 'search');
    input = el('input', 'minus-input');
    input.type = 'search';
    input.setAttribute('data-minus', 'query');
    input.setAttribute('aria-label', 'Search Minus');
    input.setAttribute('placeholder', 'Title, album, release…');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    form.appendChild(input);
    var submit = el('button', 'minus-btn minus-btn-primary', 'Search');
    submit.type = 'submit';
    form.appendChild(submit);
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      runSearch(input.value);
    });
    modal.appendChild(form);

    statusBox = el('div', 'minus-status');
    statusBox.setAttribute('data-minus', 'status');
    statusBox.setAttribute('role', 'status');
    statusBox.setAttribute('aria-live', 'polite');
    modal.appendChild(statusBox);

    jobBox = el('div', 'minus-job');
    jobBox.setAttribute('data-minus', 'job');
    jobBox.hidden = true;
    modal.appendChild(jobBox);

    resultsBox = el('div', 'minus-results');
    resultsBox.setAttribute('data-minus', 'results');
    modal.appendChild(resultsBox);

    overlay.appendChild(modal);
    overlay.addEventListener('click', function (event) {
      if (event.target === overlay) closeModal();
    });
    overlay.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeModal();
      }
    });
    document.body.appendChild(overlay);
  }

  function setStatus(text, tone) {
    statusBox.textContent = text || '';
    if (tone) statusBox.setAttribute('data-tone', tone);
    else statusBox.removeAttribute('data-tone');
  }

  function openModal(query, autoSearch) {
    lastFocused = document.activeElement;
    overlay.hidden = false;
    input.value = typeof query === 'string' ? query : '';
    setStatus('');
    window.setTimeout(function () {
      input.focus();
      input.select();
    }, 0);
    if (autoSearch && input.value.trim()) runSearch(input.value);
  }

  function closeModal() {
    overlay.hidden = true;
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  }

  // -------------------------------------------------------------- search

  var searchSeq = 0;

  function runSearch(rawQuery) {
    var query = String(rawQuery || '').trim();
    if (!query) {
      setStatus('Type something to search for.', 'error');
      input.focus();
      return;
    }
    var seq = searchSeq + 1;
    searchSeq = seq;
    clear(resultsBox);
    setStatus('Searching for “' + query + '”…', '');
    input.value = query;

    apiFetch('/api/search?q=' + encodeURIComponent(query) + '&limit=' + SEARCH_LIMIT)
      .then(function (response) {
        if (!response.ok) return failed(response);
        return response.json();
      })
      .then(function (payload) {
        if (seq !== searchSeq) return;
        renderResults(payload, query);
      })
      .catch(function (error) {
        if (seq !== searchSeq) return;
        setStatus('Search failed: ' + error.message, 'error');
      });
  }

  function renderResults(payload, query) {
    clear(resultsBox);
    if (!payload || typeof payload !== 'object') {
      setStatus('Minus sent an unreadable search response.', 'error');
      return;
    }

    var errors = Array.isArray(payload.errors) ? payload.errors : [];
    var shownErrors = [];
    for (var e = 0; e < errors.length; e += 1) {
      if (errors[e]) shownErrors.push(String(errors[e]));
    }
    if (shownErrors.length) {
      var warning = el('div', 'minus-warnings');
      for (var w = 0; w < shownErrors.length; w += 1) {
        warning.appendChild(el('div', 'minus-warning', shownErrors[w]));
      }
      resultsBox.appendChild(warning);
    }

    var candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
    var threshold = typeof payload.threshold === 'number' ? payload.threshold : null;
    if (!candidates.length) {
      setStatus(
        shownErrors.length
          ? 'No candidates found; some trackers failed (see above).'
          : 'No candidates found for “' + query + '”. Try a different title.',
        'empty'
      );
      return;
    }

    setStatus(
      candidates.length +
        ' ' +
        plural(candidates.length, 'candidate') +
        (threshold !== null ? ' · score threshold ' + formatScore(threshold) : ''),
      ''
    );
    for (var i = 0; i < candidates.length; i += 1) {
      resultsBox.appendChild(renderCandidate(candidates[i], threshold, query));
    }
  }

  function renderCandidate(candidate, threshold, query) {
    candidate = candidate || {};
    var row = el('div', 'minus-row');
    row.setAttribute('data-minus', 'row');

    var body = el('div', 'minus-row-body');
    body.appendChild(el('div', 'minus-row-title', candidate.title || '(untitled)'));

    var meta = [];
    var size = humanSize(candidate.size_bytes);
    meta.push(size || 'size unknown');
    if (typeof candidate.seeders === 'number') {
      meta.push(candidate.seeders + ' ' + plural(candidate.seeders, 'seeder'));
    } else {
      meta.push('seeders unknown');
    }
    if (typeof candidate.score === 'number') meta.push('score ' + formatScore(candidate.score));
    var provider = candidate.provider || candidate.tracker;
    meta.push(provider ? String(provider) : 'unknown provider');
    body.appendChild(el('div', 'minus-row-meta', meta.join(' · ')));

    if (Array.isArray(candidate.reasons) && candidate.reasons.length) {
      var reasons = [];
      for (var r = 0; r < candidate.reasons.length; r += 1) {
        if (candidate.reasons[r]) reasons.push(String(candidate.reasons[r]));
      }
      if (reasons.length) {
        body.appendChild(el('div', 'minus-row-reasons', 'matches: ' + reasons.join(', ')));
      }
    }

    var rejectReason = String(candidate.reject_reason || '').trim();
    var noInfohash = !candidate.infohash;
    var disabled = noInfohash || candidate.eligible === false || Boolean(rejectReason);
    if (disabled) {
      var reason =
        rejectReason || (noInfohash ? 'no infohash to grab' : 'this candidate is not eligible');
      var note = el('div', 'minus-row-note', 'Unavailable — ' + reason);
      note.setAttribute('data-tone', 'error');
      body.appendChild(note);
    }

    var lowConfidence =
      candidate.low_confidence === true ||
      (threshold !== null &&
        typeof candidate.score === 'number' &&
        candidate.score < threshold);
    if (lowConfidence) {
      body.appendChild(el('div', 'minus-row-note', 'low confidence — quarantine only'));
    }

    row.appendChild(body);

    var actions = el('div', 'minus-row-actions');
    var button = el('button', 'minus-btn minus-btn-primary', 'Download');
    button.type = 'button';
    if (disabled) {
      button.disabled = true;
      button.setAttribute(
        'aria-label',
        'Download unavailable: ' + (rejectReason || (noInfohash ? 'no infohash' : 'not eligible'))
      );
      button.title = rejectReason || (noInfohash ? 'no infohash to grab' : 'not eligible');
    } else {
      button.addEventListener('click', function () {
        grab(query, candidate, button);
      });
    }
    actions.appendChild(button);
    if (lowConfidence && !disabled) {
      actions.appendChild(el('small', '', 'downloads to quarantine for a VM back-check'));
    }
    row.appendChild(actions);

    return row;
  }

  // ---------------------------------------------------------------- grab

  function grab(query, candidate, button) {
    var previous = button.textContent;
    button.disabled = true;
    button.textContent = 'Starting…';
    setStatus('Sending “' + (candidate.title || query) + '” to the pipeline…', '');

    apiFetch('/api/grab', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: query, infohash: candidate.infohash }),
    })
      .then(function (response) {
        if (!response.ok) return failed(response);
        return response.json();
      })
      .then(function (payload) {
        if (!payload || typeof payload.job !== 'string' || !payload.job) {
          throw new Error('Minus started the grab but returned no job id.');
        }
        setStatus('Download started. Minus checks it before anything reaches the library.', 'ok');
        button.textContent = 'Downloading';
        pollJob(payload.job);
      })
      .catch(function (error) {
        setStatus('Download failed: ' + error.message, 'error');
        renderJob({
          state: 'error',
          stage: 'grab',
          detail: error.message,
        });
        button.disabled = false;
        button.textContent = previous;
      });
  }

  var pollSeq = 0;

  function pollJob(jobId) {
    pollSeq += 1;
    var seq = pollSeq;
    var started = Date.now();
    jobBox.hidden = false;
    renderJob({ id: jobId, state: 'queued', stage: 'queued', detail: 'Waiting for the pipeline…' });

    function tick() {
      if (seq !== pollSeq) return;
      if (Date.now() - started > JOB_CAP_MS) {
        renderJob({
          id: jobId,
          state: 'slow',
          stage: 'timeout',
          detail: 'Still running after 30 minutes. Check the Minus dashboard for the outcome.',
        });
        return;
      }
      apiFetch('/api/job/' + encodeURIComponent(jobId))
        .then(function (response) {
          if (!response.ok) return failed(response);
          return response.json();
        })
        .then(function (job) {
          if (seq !== pollSeq) return;
          renderJob(job);
          var state = String((job && job.state) || '').toLowerCase();
          if (state === 'done' || state === 'error') return;
          window.setTimeout(tick, POLL_MS);
        })
        .catch(function (error) {
          if (seq !== pollSeq) return;
          renderJob({
            id: jobId,
            state: 'error',
            stage: 'poll',
            detail: error.message,
          });
        });
    }

    tick();
  }

  function renderJob(job) {
    job = job || {};
    jobBox.hidden = false;
    jobBox.setAttribute('data-state', String(job.state || ''));
    clear(jobBox);

    var head = el('div', 'minus-job-head');
    head.appendChild(el('span', 'minus-job-title', 'Minus download'));
    head.appendChild(el('span', 'minus-job-state', String(job.state || 'working')));
    jobBox.appendChild(head);

    var meta = [];
    if (job.id) meta.push(String(job.id));
    if (job.stage) meta.push(String(job.stage));
    if (meta.length) jobBox.appendChild(el('div', 'minus-job-meta', meta.join(' · ')));
    if (job.detail) jobBox.appendChild(el('div', 'minus-job-detail', String(job.detail)));

    var lines = describeResult(job.result, job);
    var state = String(job.state || '').toLowerCase();
    if (!lines.length && state === 'done') lines.push({ text: 'Done.', tone: 'ok' });
    if (lines.length) {
      var box = el('div', 'minus-job-result');
      for (var i = 0; i < lines.length; i += 1) {
        var line = el('div', 'minus-job-line', lines[i].text);
        line.setAttribute('data-tone', lines[i].tone || 'muted');
        box.appendChild(line);
      }
      jobBox.appendChild(box);
    }
  }

  function describeResult(result, job) {
    var lines = [];
    var error = '';
    if (result && typeof result === 'object' && result.error) error = String(result.error);
    if (!error && String(job.state || '').toLowerCase() === 'error') {
      error = job.detail ? String(job.detail) : 'the pipeline reported an error';
    }
    if (error) lines.push({ text: 'Error: ' + error, tone: 'error' });

    if (result && typeof result === 'object') {
      var status = String(result.status || '').toLowerCase();
      var promoted = result.promoted;
      var path = firstString([
        result.promoted_path,
        result.library_path,
        result.moved_to,
        result.path,
        result.promote && result.promote.moved_to,
        result.promote && result.promote.path,
        promoted && typeof promoted === 'object' && promoted.moved_to,
        promoted && typeof promoted === 'object' && promoted.path,
      ]);

      if (path) {
        lines.push({ text: 'Added to the library: ' + path, tone: 'ok' });
      } else if (promoted === true) {
        lines.push({ text: 'Added to the library.', tone: 'ok' });
      } else if (typeof promoted === 'string' && promoted.trim()) {
        lines.push({ text: 'Added to the library: ' + promoted.trim(), tone: 'ok' });
      } else if (status === 'clean' && !result.promote_error) {
        lines.push({
          text: 'Clean — left in quarantine for review (auto-promote is off).',
          tone: 'warn',
        });
      }

      if (result.promote_error) {
        lines.push({ text: 'Promotion failed: ' + String(result.promote_error), tone: 'error' });
      }

      if (status === 'needs_review' || status === 'suspicious' || status === 'quarantined') {
        lines.push({ text: 'Held for review: ' + reviewLabel(status) + '.', tone: 'warn' });
      } else if (result.review || result.review_hold) {
        var review = result.review || result.review_hold;
        lines.push({
          text:
            typeof review === 'string' && review.trim()
              ? 'Held for review: ' + review
              : 'Held for review: the VM back-check wants a human to look.',
          tone: 'warn',
        });
      }

      if (status === 'malicious') {
        lines.push({ text: 'Malicious payload deleted; nothing was added.', tone: 'error' });
      }

      if (!lines.length) {
        var dump = safeJson(result);
        if (dump) lines.push({ text: dump, tone: 'muted' });
      }
    } else if (result !== undefined && result !== null && result !== '') {
      lines.push({ text: String(result), tone: 'muted' });
    }

    return lines;
  }

  function reviewLabel(status) {
    if (status === 'needs_review') return 'needs review';
    if (status === 'suspicious') return 'marked suspicious';
    return 'kept in quarantine';
  }

  // --------------------------------------------------- inline empty-state

  var inlineTimer = null;
  var inlineNotBefore = 0;
  var inlineAnchor = null;
  var pageObserver = null;
  var lastRouteHash = null;

  function currentSearchQuery() {
    var hash = String(window.location.hash || '');
    var match = /^#!?\/?(?:web\/)?search(?:\.html)?(?:\?(.*))?$/i.exec(hash);
    if (!match) return null;
    var params = new URLSearchParams(match[1] || '');
    return (params.get('query') || '').trim();
  }

  function hasResultCards() {
    return Boolean(
      document.querySelector(
        '.searchResults .card, .searchResults .cardBox, .searchResults .itemsContainer .card'
      )
    );
  }

  // The shipped search chunk renders div.noItemsMessage.centerMessage with
  // "No results found." (or "... for <query>") only after the server confirms
  // the result set is empty. Match on that marker instead of guessing from
  // timing; a bundle update that renames it falls back to the card-count path.
  // A marker still attached from the previous search can carry a "hide"
  // class while the next render is in flight; only a visible one proves the
  // current query found nothing. Documents without a layout engine (jsdom,
  // tests) report no client rects for everything, so fall back to the class
  // and computed display there.
  function documentHasLayout() {
    var body = document.body;
    return Boolean(
      body && typeof body.getClientRects === 'function' && body.getClientRects().length > 0
    );
  }

  function isVisible(node) {
    if (!node || !node.isConnected) return false;
    if (node.classList && node.classList.contains('hide')) return false;
    if (documentHasLayout()) return node.getClientRects().length > 0;
    var style = window.getComputedStyle ? window.getComputedStyle(node) : null;
    if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
    return true;
  }

  function findNoResultsMarker() {
    var nodes = document.querySelectorAll('.noItemsMessage');
    for (var i = 0; i < nodes.length; i += 1) {
      if (!isVisible(nodes[i])) continue;
      var text = String(nodes[i].textContent || '');
      if (/no results found/i.test(text)) return nodes[i];
    }
    return null;
  }

  function removeInline() {
    var bars = document.querySelectorAll('[data-minus="inline"]');
    for (var i = 0; i < bars.length; i += 1) {
      if (bars[i].parentNode) bars[i].parentNode.removeChild(bars[i]);
    }
    inlineAnchor = null;
    if (pageObserver) pageObserver.takeRecords();
  }

  function insertInline(target, query, after) {
    var bar = el('div', 'minus-inline');
    bar.setAttribute('data-minus', 'inline');
    bar.setAttribute('data-query', query);
    bar.setAttribute('role', 'note');
    bar.appendChild(el('span', 'minus-inline-text', 'Nothing in your library — Minus may have it'));
    var button = el('button', 'minus-btn minus-btn-primary', 'Search Minus');
    button.type = 'button';
    button.addEventListener('click', function () {
      openModal(query, true);
    });
    bar.appendChild(button);
    if (after && target.parentNode) {
      inlineAnchor = target;
      target.parentNode.insertBefore(bar, target.nextSibling);
    } else {
      inlineAnchor = null;
      target.appendChild(bar);
    }
    // Drop the mutation record for our own insertion so the observer cannot
    // mistake it for a page render and start chasing itself.
    if (pageObserver) pageObserver.takeRecords();
  }

  function nodeIsMinus(node) {
    if (!node) return false;
    if (node.nodeType !== 1) return false;
    if (node.hasAttribute && node.hasAttribute('data-minus')) return true;
    return Boolean(node.closest && node.closest('[data-minus]'));
  }

  function mutationIsOurs(record) {
    if (nodeIsMinus(record.target)) return true;
    var nodes = [record.addedNodes, record.removedNodes];
    for (var list = 0; list < nodes.length; list += 1) {
      for (var i = 0; i < nodes[list].length; i += 1) {
        if (nodeIsMinus(nodes[list][i])) return true;
      }
    }
    return false;
  }

  function scheduleInlineCheck(delay) {
    if (inlineTimer) window.clearTimeout(inlineTimer);
    inlineTimer = window.setTimeout(evaluateInline, delay === undefined ? INLINE_SETTLE : delay);
  }

  function evaluateInline() {
    inlineTimer = null;
    var query = currentSearchQuery();
    if (query === null || !query) {
      removeInline();
      return;
    }
    if (hasResultCards()) {
      removeInline();
      return;
    }
    // The no-results element is re-rendered away on the next search; once the
    // anchor we sat behind is gone the bar must go too, not linger.
    if (inlineAnchor && !isVisible(inlineAnchor)) removeInline();

    var existing = document.querySelector('[data-minus="inline"]');
    if (existing && existing.getAttribute('data-query') !== query) {
      removeInline();
      existing = null;
    }

    // Wait out the route settle window even when a marker is already on the
    // page: during an SPA transition the previous search's marker can still be
    // attached while the new query renders.
    var wait = inlineNotBefore - Date.now();
    if (wait > 0) {
      scheduleInlineCheck(wait);
      return;
    }
    if (existing) return;

    var marker = findNoResultsMarker();
    if (marker) {
      insertInline(marker, query, true);
      return;
    }

    // Secondary path: no marker yet (or a future bundle renamed it). A search
    // page with no cards after the grace period is treated as empty; the
    // observer removes the bar again the moment cards show up.
    var target =
      document.querySelector('.searchResults') ||
      document.querySelector('.libraryPage') ||
      document.querySelector('main');
    if (!target || !target.isConnected) return;
    insertInline(target, query, false);
  }

  function onRouteChange() {
    lastRouteHash = String(window.location.hash || '');
    removeInline();
    inlineNotBefore = Date.now() + INLINE_DELAY;
    if (inlineTimer) window.clearTimeout(inlineTimer);
    if (currentSearchQuery() !== null) {
      inlineTimer = window.setTimeout(evaluateInline, INLINE_DELAY);
    }
  }

  // Some hash routers push state without firing hashchange. A render that
  // lands under a different hash is still a navigation, so treat it as one.
  function checkRouteChange() {
    var hash = String(window.location.hash || '');
    if (hash === lastRouteHash) return false;
    onRouteChange();
    return true;
  }

  function watchPage() {
    pageObserver = new MutationObserver(function (records) {
      if (checkRouteChange()) return;
      var relevant = false;
      for (var i = 0; i < records.length; i += 1) {
        if (!mutationIsOurs(records[i])) {
          relevant = true;
          break;
        }
      }
      if (!relevant) return;
      if (currentSearchQuery() === null) {
        removeInline();
        return;
      }
      if (inlineAnchor && !isVisible(inlineAnchor)) {
        removeInline();
        return;
      }
      scheduleInlineCheck();
    });
    pageObserver.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['class'],
    });
  }

  // ---------------------------------------------------------------- boot

  function boot() {
    if (document.querySelector('[data-minus="fab"]')) return;
    injectStyles();
    buildLauncher();
    buildModal();
    watchPage();
    window.addEventListener('hashchange', onRouteChange);
    window.addEventListener('popstate', onRouteChange);
    lastRouteHash = String(window.location.hash || '');
    inlineNotBefore = Date.now() + INLINE_DELAY;
    if (currentSearchQuery() !== null) {
      inlineTimer = window.setTimeout(evaluateInline, INLINE_DELAY);
    }
  }

  if (document.body) {
    boot();
  } else {
    document.addEventListener('DOMContentLoaded', boot);
  }
})();
