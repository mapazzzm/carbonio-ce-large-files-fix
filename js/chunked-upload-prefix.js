/* Carbonio Files chunked-upload monkey-patch (v50).
 *
 * Interception points (per-target, not global — global window.XMLHttpRequest
 * patching breaks Carbonio's "Open Trash" snackbar):
 *   1. window.__cuXHR  — wrapper for `new XMLHttpRequest` in 659.*.chunk.js
 *      (sed-replaced to `new (window.__cuXHR||XMLHttpRequest)`).
 *   2. window.__cuFetch — wrapper for fetch() in mails-edit-view.*.chunk.js
 *      (smart-link mail attach uses fetch, not XHR).
 *   3. document drop / change listeners (capture phase) — pre-register
 *      dropped files into the overlay so the user sees "N of M" immediately,
 *      including Carbonio's LIMIT:16 queue tail.
 *
 * Concurrency: chunked uploads use dynamic worker count =
 *   max(1, floor(BrowserSockets / (active_sessions + 1)))
 * 1 file → 5 workers (full bandwidth); 5 files → 1 each (visible parallel
 * progress). Computed once per session start; not re-balanced mid-flight.
 *
 * Overlay design:
 *   - Probes a live Carbonio dialog/modal to copy bg/text/font/radius, so it
 *     looks like a native component. Falls back to a theme-adaptive default.
 *   - `pointer-events: none` so it never intercepts clicks on Carbonio's own
 *     snackbar buttons appearing in the same corner.
 *   - Hidden while sessions are still 'queued' (e.g. mail compose waiting on
 *     "Convert to smart link?" confirmation) — shows only when actually
 *     uploading.
 *
 * Post-upload: targeted Apollo cache.modify deletes only
 *   getNode({"node_id":"<ParentId>"})
 * for the folder we just wrote into, forcing Apollo to re-fetch on next
 * render. Sibling `getPath` / `getNode(other_folder)` entries are left intact
 * so the user's open folder tree survives.
 */
(function () {
  if (window.__cuPatched) return;
  window.__cuPatched = true;

  // Detect which Carbonio app chunk loaded us. document.currentScript points
  // to the currently-executing <script src=...> while the synchronous portion
  // of that script runs (including this IIFE). We need this so we don't load
  // files-ui's uploadView chunk into a mails-ui React context, which would
  // mount a second PostHogProvider and trigger PostHog's "already loaded"
  // warning. Default to "files-ui" if detection fails (safest, preserves
  // existing behavior for the historical install path).
  var __cuContext = 'files-ui';
  try {
    var __src = (document.currentScript && document.currentScript.src) || '';
    if (__src.indexOf('/carbonio-mails-ui/') !== -1) __cuContext = 'mails-ui';
    else if (__src.indexOf('/carbonio-files-ui/') !== -1) __cuContext = 'files-ui';
  } catch (e) {}

  // Filter Carbonio's "[PostHog.js] posthog was already loaded elsewhere"
  // warning. Cause is architectural (each Carbonio app bundles its own
  // PostHogProvider, so whichever mounts second prints the warning); our
  // prefix doesn't load posthog. The warning has no functional effect —
  // we keep the console clean by suppressing only this exact message.
  try {
    var __origWarn = console.warn;
    console.warn = function () {
      var first = arguments[0];
      if (typeof first === 'string' &&
          first.indexOf('[PostHog.js]') === 0 &&
          first.indexOf('already loaded elsewhere') !== -1) {
        return;
      }
      return __origWarn.apply(console, arguments);
    };
  } catch (e) {}

  var Chunk = 100 * 1024 * 1024;
  var Threshold = 200 * 1024 * 1024;            // chunked path threshold
  var LargeFileThreshold = 50 * 1024 * 1024;    // plain-upload "large" cutoff
  var BrowserSockets = 5;          // ≈ Chrome HTTP/1.1 limit per origin, share fairly
  var SessionConcurrency = 5;
  var LargePlainConcurrency = 5;   // gate for plain uploads in [50MB, 200MB]
  var ProgressThrottleMs = 250;
  var UploadPhaseRatio = 0.95;
  var FinalisingPollMs = 1500;

  // Dynamic worker count: counts other 'uploading' sessions and splits
  // BrowserSockets fairly among (others + self). 1 active file → 5 workers
  // (full speed); 2 → 2 each; 5 → 1 each. Already-started files keep their
  // worker count — re-balancing isn't worth the race-condition complexity.
  function calcWorkers(forSid) {
    var others = 0;
    for (var k in sessions) {
      if (k !== forSid && sessions[k].status === 'uploading') others++;
    }
    return Math.max(1, Math.floor(BrowserSockets / (others + 1)));
  }

  var sessionGate = { inflight: 0, waiters: [], max: SessionConcurrency };
  function sessionAcquire() {
    return new Promise(function (resolve) {
      if (sessionGate.inflight < sessionGate.max) { sessionGate.inflight++; resolve(); }
      else sessionGate.waiters.push(resolve);
    });
  }
  function sessionRelease() {
    var w = sessionGate.waiters.shift();
    if (w) w(); else sessionGate.inflight--;
  }

  // Plain-upload gate for files in (LargeFileThreshold, Threshold]. Small
  // files run free (Carbonio LIMIT:16 + browser HTTP/1.1 socket cap ~6 means
  // ~6 concurrent transfers, smooth progress). Medium-large files would
  // saturate memory and split bandwidth if all started at once — gate to 5.
  var largePlainGate = { inflight: 0, waiters: [], max: LargePlainConcurrency };
  function largePlainAcquire() {
    return new Promise(function (resolve) {
      if (largePlainGate.inflight < largePlainGate.max) { largePlainGate.inflight++; resolve(); }
      else largePlainGate.waiters.push(resolve);
    });
  }
  function largePlainRelease() {
    var w = largePlainGate.waiters.shift();
    if (w) w(); else largePlainGate.inflight--;
  }

  var sessions = {};

  // Sample Carbonio's live UI: probe an open dialog/modal/menu/dropdown and
  // copy its computed style (bg, color, font, radius) so our overlay looks
  // pixel-identical to e.g. the "Convert to smart link?" dialog. If none is
  // open, fall through to a theme-adaptive default (walk DOM, skip
  // transparent — body in Carbonio is often `rgba(0,0,0,0)`).
  function parseRgb(str) {
    var m = String(str || '').match(/[\d.]+/g);
    if (!m || m.length < 3) return null;
    if (m.length >= 4 && parseFloat(m[3]) === 0) return null;
    return [+m[0], +m[1], +m[2]];
  }
  function lumaOf(rgb) { return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255; }

  function detectPalette() {
    var dark = {
      bg: '#252a30', text: '#dfdfdf', textMuted: 'rgba(255,255,255,0.6)',
      primary: '#2b73d2', trackBg: 'rgba(255,255,255,0.15)',
      border: 'none', shadow: '0 6px 20px rgba(0,0,0,0.45)',
      borderRadius: '8px', fontFamily: 'Roboto, system-ui, sans-serif'
    };
    var light = {
      bg: '#ffffff', text: '#414141', textMuted: '#828282',
      primary: '#2b73d2', trackBg: '#ebeef2',
      border: '1px solid #d4d7dd', shadow: '0 4px 16px rgba(0,0,0,0.12)',
      borderRadius: '8px', fontFamily: 'Roboto, system-ui, sans-serif'
    };
    // 1. Probe: copy from an open Carbonio dialog/modal/dropdown.
    try {
      var probes = [
        '[role="dialog"][aria-modal="true"]',
        '[role="dialog"]',
        '[class*="ModalContainer"]',
        '[class*="ModalContent"]',
        '[class*="DropdownContainer"]',
        '[class*="DropdownPopper"]'
      ];
      for (var i = 0; i < probes.length; i++) {
        var el = document.querySelector(probes[i]);
        if (!el || el.id === '__cu_overlay') continue;
        var cs = getComputedStyle(el);
        var rgb = parseRgb(cs.backgroundColor);
        if (!rgb) continue;
        var dk = lumaOf(rgb) < 0.5;
        var base = dk ? dark : light;
        return {
          bg: cs.backgroundColor,
          text: cs.color || base.text,
          textMuted: dk ? 'rgba(255,255,255,0.6)' : '#828282',
          primary: '#2b73d2',
          trackBg: dk ? 'rgba(255,255,255,0.15)' : '#ebeef2',
          border: 'none',
          shadow: dk ? '0 6px 20px rgba(0,0,0,0.45)' : '0 4px 16px rgba(0,0,0,0.12)',
          borderRadius: cs.borderRadius && cs.borderRadius !== '0px' ? cs.borderRadius : '8px',
          fontFamily: cs.fontFamily || base.fontFamily
        };
      }
    } catch (e) {}
    // 2. Fallback: detect theme from app/main/body background luminance.
    try {
      var candidates = [
        document.querySelector('#app'),
        document.querySelector('[data-testid="app"]'),
        document.querySelector('main'),
        document.body,
        document.documentElement
      ];
      for (var j = 0; j < candidates.length; j++) {
        var c = candidates[j];
        if (!c) continue;
        var rgb2 = parseRgb(getComputedStyle(c).backgroundColor);
        if (!rgb2) continue;
        return lumaOf(rgb2) < 0.5 ? dark : light;
      }
    } catch (e) {}
    return light;
  }

  function ensureOverlay() {
    var el = document.getElementById('__cu_overlay');
    if (el) return el;
    var p = detectPalette();
    el = document.createElement('div');
    el.id = '__cu_overlay';
    el.setAttribute('style',
      'position:fixed;right:16px;bottom:16px;z-index:2147483647;' +
      'background:' + p.bg + ';color:' + p.text + ';' +
      (p.border !== 'none' ? 'border:' + p.border + ';' : '') +
      'border-radius:' + (p.borderRadius || '8px') + ';' +
      'padding:14px 16px;min-width:280px;max-width:360px;' +
      'font-family:' + (p.fontFamily || 'Roboto, system-ui, sans-serif') + ';' +
      'font-size:13px;line-height:1.4;' +
      'box-shadow:' + p.shadow + ';' +
      // pointer-events:none — overlay must not intercept clicks for
      // Carbonio snackbars that appear at the same bottom-right corner.
      'pointer-events:none;' +
      'transition:opacity 0.2s;');
    el.innerHTML =
      '<div id="__cu_overlay_header" style="font-weight:500;font-size:13px;' +
        'color:' + p.text + ';margin-bottom:6px;white-space:nowrap;' +
        'overflow:hidden;text-overflow:ellipsis;">Загрузка</div>' +
      '<div id="__cu_overlay_sub" style="font-size:11px;color:' + p.textMuted + ';' +
        'margin-bottom:10px;">0%</div>' +
      '<div style="background:' + p.trackBg + ';border-radius:2px;height:4px;' +
        'overflow:hidden;">' +
        '<div id="__cu_overlay_total_bar" style="height:100%;width:0%;' +
          'background:' + p.primary + ';transition:width 0.25s ease-out;"></div>' +
      '</div>';
    (document.body || document.documentElement).appendChild(el);
    return el;
  }

  // ---------- i18n -----------------------------------------------------
  // Carbonio locale → ru/en (Russian for ru*, English for everything else).
  function detectLocale() {
    try {
      var l = (document.documentElement.lang || navigator.language || 'en').toLowerCase();
      return l.indexOf('ru') === 0 ? 'ru' : 'en';
    } catch (e) { return 'en'; }
  }
  var T = {
    ru: {
      fallbackName: 'Файл',
      uploading: function (n) {
        var n10 = n % 10, n100 = n % 100, w;
        if (n10 === 1 && n100 !== 11) w = 'файл';
        else if (n10 >= 2 && n10 <= 4 && (n100 < 10 || n100 >= 20)) w = 'файла';
        else w = 'файлов';
        return 'Загрузка ' + n + ' ' + w;
      },
      done: 'готово',
      failed: 'ошибка',
      merging: 'сборка',
      completedOf: function (done, total) { return 'завершено ' + done + ' из ' + total; }
    },
    en: {
      fallbackName: 'File',
      uploading: function (n) { return 'Uploading ' + n + (n === 1 ? ' file' : ' files'); },
      done: 'done',
      failed: 'failed',
      merging: 'merging',
      completedOf: function (done, total) { return 'completed ' + done + ' of ' + total; }
    }
  };
  var L = T[detectLocale()];

  function refreshOverlay() {
    var ids = Object.keys(sessions);
    var el = document.getElementById('__cu_overlay');
    // Hide if no sessions at all OR every session is still 'queued' (pre-
    // registered from a drop event, but the actual upload hasn't started —
    // e.g., Carbonio Mail is showing a "convert to smart link?" prompt).
    var hasActiveOrDone = false;
    for (var i = 0; i < ids.length; i++) {
      var st = sessions[ids[i]].status;
      if (st !== 'queued') { hasActiveOrDone = true; break; }
    }
    if (!hasActiveOrDone) {
      if (el) el.style.opacity = '0';
      return;
    }
    el = ensureOverlay();
    // Include EVERY session in the count (queued + uploading + done/failed),
    // so the overlay shows the user's full batch from the moment they drop
    // it — e.g. 16 dropped files immediately read "Загрузка 16 файлов"
    // instead of climbing 5 → 10 → 15 → 16 as Carbonio's LIMIT:16 dequeues.
    var totalSize = 0, totalLoaded = 0, doneCount = 0, totalCount = ids.length;
    for (var j = 0; j < ids.length; j++) {
      var s = sessions[ids[j]];
      totalSize += (s.size || 0);
      totalLoaded += (s.loaded || 0); // 0 for queued sessions
      if (s.status === 'done' || s.status === 'failed') doneCount++;
    }
    var pct = totalSize > 0 ? Math.min(100, Math.floor(totalLoaded / totalSize * 100)) : 0;
    var header = document.getElementById('__cu_overlay_header');
    var sub = document.getElementById('__cu_overlay_sub');
    var totalBar = document.getElementById('__cu_overlay_total_bar');

    if (totalCount === 1) {
      var only = sessions[ids[0]];
      var phaseLbl = only.status === 'done' ? L.done :
                     only.status === 'failed' ? L.failed :
                     only.phase === 'merging' ? L.merging : '';
      header.textContent = only.name || L.fallbackName;
      sub.textContent = pct + '%' + (phaseLbl ? ' · ' + phaseLbl : '');
    } else {
      header.textContent = L.uploading(totalCount);
      sub.textContent = pct + '% · ' + L.completedOf(doneCount, totalCount);
    }
    totalBar.style.width = pct + '%';
    el.style.opacity = '1';
  }

  function sessionStart(sid, name, size) {
    sessions[sid] = { name: name, size: size, loaded: 0, status: 'queued', phase: '' };
    refreshOverlay();
  }
  function sessionActive(sid) {
    if (sessions[sid]) { sessions[sid].status = 'uploading'; refreshOverlay(); }
  }
  function sessionProgress(sid, loaded, phase) {
    if (sessions[sid]) {
      sessions[sid].loaded = loaded;
      if (phase != null) sessions[sid].phase = phase;
      refreshOverlay();
    }
  }
  function sessionEnd(sid, success) {
    if (sessions[sid]) {
      sessions[sid].status = success ? 'done' : 'failed';
      sessions[sid].loaded = sessions[sid].size;
      refreshOverlay();
      maybeFlushBatch();
    }
  }

  // Schedule a delayed cleanup of finished sessions. We flush ONLY 'done'
  // and 'failed' sessions and leave any 'queued' sessions alone — those will
  // be reaped by their own PENDING_TTL_MS timeout. This handles the case
  // where pre-registration (via drop/change) creates a queued session whose
  // claimPending() never gets matched (e.g. mail compose re-wraps the File
  // as a plain Blob without a .name, so the name+size key doesn't match).
  // Without this split, a stranded queued session would pin the overlay open
  // forever showing the previous "100% done" state.
  var flushTimer = null;
  function maybeFlushBatch() {
    var ids = Object.keys(sessions);
    if (ids.length === 0) return;
    for (var i = 0; i < ids.length; i++) {
      var st = sessions[ids[i]].status;
      if (st !== 'done' && st !== 'failed' && st !== 'queued') {
        // Something is still uploading — defer.
        if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
        return;
      }
    }
    if (flushTimer) clearTimeout(flushTimer);
    flushTimer = setTimeout(function () {
      flushTimer = null;
      var keys = Object.keys(sessions);
      // If a new upload started during the timeout, leave things alone.
      for (var j = 0; j < keys.length; j++) {
        var s = sessions[keys[j]].status;
        if (s !== 'done' && s !== 'failed' && s !== 'queued') return;
      }
      // Drop terminal sessions only. Queued sessions keep their own TTL.
      for (var k = 0; k < keys.length; k++) {
        var ss = sessions[keys[k]].status;
        if (ss === 'done' || ss === 'failed') delete sessions[keys[k]];
      }
      refreshOverlay();
    }, 5000);
  }

  // Carbonio has multiple Apollo clients (one per app: files-ui, mails-ui,
  // shell-ui, etc). After upload, we want to refetch on ALL of them — e.g.
  // smart-link upload from compose finishes in mails-ui context; the user
  // then navigates to Files Home, which uses files-ui's Apollo cache and
  // would otherwise show stale data until F5.
  function findAllApolloClients() {
    var found = [];
    var seenClients = new WeakSet ? new WeakSet() : { has: function () { return false; }, add: function () {} };
    var seenFibers = new WeakSet ? new WeakSet() : { has: function () { return false; }, add: function () {} };
    // Walk the *entire* DOM for any element with a React fiber root attached
    // (not just `div,section,main` — Carbonio mounts apps under various tags
    // including modals/portals via Carbonio shell).
    var roots = [];
    var all = document.getElementsByTagName('*');
    for (var i = 0; i < all.length; i++) {
      for (var k in all[i]) {
        if (k.indexOf('__reactContainer$') === 0 ||
            k.indexOf('__reactInternalInstance$') === 0) {
          roots.push(all[i][k]); break;
        }
      }
    }
    function walk(f, d) {
      if (!f || d > 800 || seenFibers.has(f)) return;
      seenFibers.add(f);
      var c = null;
      if (f.stateNode && typeof f.stateNode === 'object' &&
          f.stateNode.client && typeof f.stateNode.client.refetchQueries === 'function') {
        c = f.stateNode.client;
      } else if (f.memoizedProps && f.memoizedProps.client &&
          typeof f.memoizedProps.client.refetchQueries === 'function') {
        c = f.memoizedProps.client;
      }
      if (c && !seenClients.has(c)) { seenClients.add(c); found.push(c); }
      walk(f.child, d + 1);
      walk(f.sibling, d + 1);
    }
    for (var r = 0; r < roots.length; r++) walk(roots[r], 0);
    if (window.__APOLLO_CLIENT__ && !seenClients.has(window.__APOLLO_CLIENT__)) {
      found.push(window.__APOLLO_CLIENT__);
    }
    return found;
  }

  // Refresh Files UI after an upload. We have to be SURGICAL — Carbonio's
  // Apollo cache uses `getNode({"node_id":"<id>"})` for both folder content
  // AND folder tree navigation. We want to invalidate the *folder we just
  // uploaded into* (so it re-fetches the new child) without touching the
  // surrounding folder tree (which would lose the user's open subtree).
  //
  // `parentId` is the value of the upload's `ParentId` header (LOCAL_ROOT for
  // smart-link uploads from mail compose, the current folder for in-Files
  // drag-drops). If parentId is missing, fall back to refetching active
  // observable queries only (best-effort, won't help for cached lists).
  // Batch-aware refresh: when multiple files are uploaded to the same folder,
  // calling softRefreshFolderView() per file races against the server (which
  // is still wiring up newly-created nodes' revision/blob metadata). Apollo
  // refetches mid-flight and gets null for required fields (size, mime_type,
  // version) -> "Could not find version: 1" + "non-nullable returned null"
  // GraphQL errors -> Carbonio shows a yellow snackbar. So we queue parentIds
  // and only flush after every active session is done — one refresh per batch.
  var pendingParentIds = {};
  var refreshFlushTimer = null;
  function queueFolderRefresh(parentId) {
    if (!parentId) return;
    pendingParentIds[parentId] = true;
    scheduleRefreshFlush();
  }
  function scheduleRefreshFlush() {
    if (refreshFlushTimer) clearTimeout(refreshFlushTimer);
    refreshFlushTimer = setTimeout(function () {
      refreshFlushTimer = null;
      // If any session is still uploading, reschedule — we want a single
      // refresh once the whole batch has settled.
      for (var sid in sessions) {
        if (sessions[sid].status === 'uploading') {
          scheduleRefreshFlush();
          return;
        }
      }
      var ids = Object.keys(pendingParentIds);
      pendingParentIds = {};
      for (var i = 0; i < ids.length; i++) softRefreshFolderView(ids[i]);
    }, 800);
  }

  function softRefreshFolderView(parentId) {
    // Targeted cache invalidation only. We intentionally do NOT call
    // refetchQueries() or reFetchObservableQueries() — those try to re-execute
    // every active query, including Apollo *local-only* state (e.g.
    // `getUploadItem` in files-ui's UploadStore), which the GraphQL server
    // doesn't know about and rejects with "Field 'getUploadItem' in type
    // 'Query' is undefined". Three belt-and-braces evictions per client:
    //   (a) ROOT_QUERY.getNode({"node_id":parentId}) — the query result entry
    //   (b) Folder:<parentId> — the normalised entity (its `children` field)
    //   (c) cache.modify DELETE for any storeFieldName variant we missed
    // Apollo's observers will refetch on the next render, and only the real
    // server query (`getNode`) goes over the wire.
    if (!parentId) return;
    try {
      var clients = findAllApolloClients();
      var target = 'getNode({"node_id":"' + parentId + '"})';
      for (var i = 0; i < clients.length; i++) {
        var c = clients[i];
        if (!c.cache) continue;
        try {
          if (typeof c.cache.evict === 'function') {
            c.cache.evict({ id: 'ROOT_QUERY', fieldName: 'getNode', args: { node_id: parentId } });
            c.cache.evict({ id: 'Folder:' + parentId });
          }
          if (typeof c.cache.modify === 'function') {
            c.cache.modify({
              fields: {
                getNode: function (existing, h) {
                  return h.storeFieldName === target ? h.DELETE : existing;
                }
              }
            });
          }
          if (typeof c.cache.gc === 'function') c.cache.gc();
        } catch (e) {}
      }
    } catch (e) { console.warn('[chunked-upload] apollo refresh failed:', e); }
  }

  // Per-instance wrapper. We never touch window.XMLHttpRequest, only the
  // upload sites of 659.*.chunk.js opt in via `new (window.__cuXHR||...)`.
  var OrigXHR = window.XMLHttpRequest;

  window.__cuXHR = function PatchedXHR() {
    var xhr = new OrigXHR();
    var meta = { headers: {} };
    var origOpen = xhr.open.bind(xhr);
    var origSetHeader = xhr.setRequestHeader.bind(xhr);
    var origSend = xhr.send.bind(xhr);

    xhr.open = function (method, url) {
      meta.method = method; meta.url = url;
      return origOpen.apply(this, arguments);
    };
    xhr.setRequestHeader = function (n, v) {
      meta.headers[n] = v;
      return origSetHeader.apply(this, arguments);
    };
    xhr.send = function (body) {
      var sendArgs = arguments;
      if (meta.method && meta.method.toUpperCase() === 'POST' &&
          meta.url && /\/services\/files\/upload(\?|$)/.test(meta.url) &&
          body instanceof Blob) {
        if (body.size > Threshold) {
          try { return chunkedSend(xhr, body, meta); } catch (e) { console.error('[chunked-upload] fallback:', e); }
        } else if (body.size > LargeFileThreshold) {
          // Medium-large plain upload — gate through largePlainGate so
          // bandwidth is shared sanely across at most LargePlainConcurrency.
          try { trackPlainUpload(xhr, body, meta); } catch (e) {}
          xhr.addEventListener('loadend', largePlainRelease);
          largePlainAcquire().then(function () { origSend.apply(xhr, sendArgs); });
          return;
        } else {
          // Small file — run free. Carbonio LIMIT:16 + browser HTTP/1.1
          // socket cap handles concurrency naturally.
          try { trackPlainUpload(xhr, body, meta); } catch (e) {}
        }
      }
      return origSend.apply(this, arguments);
    };

    return xhr;
  };

  // Fetch adapter: mails-edit-view smart-link upload uses fetch(), not XHR.
  // Sed-replaced `fetch(...)` in that chunk to `(window.__cuFetch||fetch)(...)`,
  // so this wrapper converts a fetch call into XHR via __cuXHR (chunked logic
  // and overlay tracking apply). Returns a Response-like object.
  window.__cuFetch = function (url, opts) {
    opts = opts || {};
    return new Promise(function (resolve, reject) {
      var xhr = new window.__cuXHR();
      xhr.open(opts.method || 'GET', url, true);
      if (opts.headers) {
        for (var k in opts.headers) {
          if (Object.prototype.hasOwnProperty.call(opts.headers, k)) {
            try { xhr.setRequestHeader(k, opts.headers[k]); } catch (e) {}
          }
        }
      }
      if (opts.signal) {
        if (opts.signal.aborted) { reject(new DOMException('Aborted', 'AbortError')); return; }
        opts.signal.addEventListener('abort', function () { try { xhr.abort(); } catch (e) {} });
      }
      xhr.addEventListener('load', function () {
        var body = xhr.responseText || '';
        var hdr = {};
        try {
          (xhr.getAllResponseHeaders() || '').split(/\r?\n/).forEach(function (line) {
            var i = line.indexOf(':');
            if (i > 0) hdr[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
          });
        } catch (e) {}
        resolve({
          ok: xhr.status >= 200 && xhr.status < 300,
          status: xhr.status,
          statusText: xhr.statusText || '',
          headers: { get: function (n) { return hdr[(n || '').toLowerCase()] || null; } },
          json: function () { try { return Promise.resolve(JSON.parse(body)); } catch (e) { return Promise.reject(e); } },
          text: function () { return Promise.resolve(body); },
          blob: function () { return Promise.resolve(new Blob([body])); }
        });
      });
      xhr.addEventListener('error', function () { reject(new TypeError('Network error')); });
      xhr.addEventListener('abort', function () { reject(new DOMException('Aborted', 'AbortError')); });
      xhr.send(opts.body);
    });
  };
  // ---------- Pre-registration via drop/picker ---------------------------
  // We want every dropped file in the overlay immediately — including the
  // ones Carbonio is still keeping in its own queue (LIMIT:16). We listen on
  // document `drop` (capture phase, before Carbonio's React handlers) and on
  // <input type=file> change events. The session id we assign is later
  // matched against the XHR body in P.send by (name + size).
  var pendingFiles = []; // { sid, key, ts }
  var PENDING_TTL_MS = 5 * 60 * 1000;

  function preRegister(file) {
    if (!file || typeof file.size !== 'number') return null;
    var sid = 'q' + Math.random().toString(36).slice(2) + Date.now();
    var key = (file.name || '?') + '|' + file.size;
    sessionStart(sid, file.name || 'file', file.size);
    pendingFiles.push({ sid: sid, key: key, ts: Date.now() });
    // If the upload never actually starts (e.g. the user cancels Carbonio
    // Mail's "convert to smart link?" dialog or aborts a confirmation),
    // the queued session would otherwise linger in `sessions{}`. Clean it
    // up after PENDING_TTL_MS so it doesn't accumulate.
    setTimeout(function () {
      if (sessions[sid] && sessions[sid].status === 'queued') {
        delete sessions[sid];
        refreshOverlay();
      }
    }, PENDING_TTL_MS);
    return sid;
  }

  function claimPending(file) {
    // Drop stale entries first
    var now = Date.now();
    pendingFiles = pendingFiles.filter(function (p) { return now - p.ts < PENDING_TTL_MS; });
    var key = ((file && file.name) || '?') + '|' + (file && file.size);
    for (var i = 0; i < pendingFiles.length; i++) {
      if (pendingFiles[i].key === key) {
        var sid = pendingFiles[i].sid;
        pendingFiles.splice(i, 1);
        return sid;
      }
    }
    return null;
  }

  // On the FIRST drop, eager-load uploadView via <script>. We deliberately
  // wait for a user action so we don't conflict with Carbonio's PostHogProvider
  // initialisation on initial render (re-loading the chunk while React is
  // still wiring up providers breaks the "Open Trash" snackbar button).
  var uploadViewLoaded = false;
  function ensureUploadViewLoaded() {
    if (uploadViewLoaded) return;
    uploadViewLoaded = true;
    // Skip in mails-ui context — loading files-ui's uploadView chunk into
    // a mails-ui React tree mounts a second PostHogProvider, which prints
    // a "[PostHog.js] posthog was already loaded elsewhere" warning. The
    // fast "Downloads" tab transition only matters in Files anyway.
    if (__cuContext === 'mails-ui') return;
    try {
      if (document.querySelector('script[data-cu-uploadview]') ||
          document.querySelector('script[src*="uploadView."]')) return;
      var sameOriginScript = document.querySelector('script[src*="/carbonio-files-ui/"]');
      if (!sameOriginScript) return;
      var m = sameOriginScript.src.match(/^(.+\/carbonio-files-ui\/[^/]+\/)/);
      if (!m) return;
      var sc = document.createElement('script');
      sc.src = m[1] + 'uploadView.0c7dbda7.chunk.js';
      sc.async = true; sc.defer = true;
      sc.crossOrigin = sameOriginScript.crossOrigin || 'anonymous';
      sc.setAttribute('data-cu-uploadview', '1');
      document.head.appendChild(sc);
    } catch (e) { console.warn('[chunked-upload] uploadView load failed:', e); }
  }

  document.addEventListener('drop', function (e) {
    try {
      if (!e.dataTransfer || !e.dataTransfer.files) return;
      var files = e.dataTransfer.files;
      if (files.length > 0) ensureUploadViewLoaded();
      for (var i = 0; i < files.length; i++) preRegister(files[i]);
    } catch (err) { console.warn('[chunked-upload] drop pre-register failed:', err); }
  }, true);

  function attachInputListener(input) {
    if (input.__cuTracked) return;
    input.__cuTracked = true;
    input.addEventListener('change', function () {
      if (!input.files) return;
      for (var i = 0; i < input.files.length; i++) preRegister(input.files[i]);
    });
  }
  // We deliberately do NOT install a MutationObserver on document — it fires
  // on every DOM mutation and interferes with React's snackbar/toast rendering
  // (the "Open Trash" button stops responding). Instead we listen on
  // document `change` via event delegation, which catches input[type=file]
  // events without observing the whole tree.
  document.addEventListener('change', function (e) {
    var t = e.target;
    if (t && t.tagName === 'INPUT' && t.type === 'file' && t.files && t.files.length > 0) {
      ensureUploadViewLoaded();
      for (var i = 0; i < t.files.length; i++) preRegister(t.files[i]);
    }
  }, true);

  function trackPlainUpload(xhr, file, meta) {
    // Plain (non-chunked) upload: keep it in the overlay so the user sees the
    // *overall* progress of all dropped files. Reuse the pre-registered
    // session id if drop/picker already added this file; otherwise allocate.
    var sid = claimPending(file) ||
              ('p' + Math.random().toString(36).slice(2) + Date.now());
    if (!sessions[sid]) sessionStart(sid, file.name || 'file', file.size);
    sessionActive(sid);
    xhr.upload.addEventListener('progress', function (e) {
      sessionProgress(sid, e.loaded || 0, '');
    });
    xhr.addEventListener('load', function () {
      var ok = xhr.status >= 200 && xhr.status < 300;
      sessionEnd(sid, ok);
      if (ok) {
        var h = (meta && meta.headers) || {};
        queueFolderRefresh(h.ParentId || h.parentid || h.parentId);
      }
    });
    xhr.addEventListener('error', function () { sessionEnd(sid, false); });
    xhr.addEventListener('abort', function () { sessionEnd(sid, false); });
  }

  function chunkedSend(xhr, file, meta) {
    // Reuse pre-registered session id (from document drop / file input change)
    // if available, so the overlay shows the right "N of M" count from the
    // moment the user dropped the batch, not when the XHR actually starts.
    var sid = claimPending(file) ||
              ('s' + Math.random().toString(36).slice(2) + Date.now()).slice(0, 32);
    var total = Math.ceil(file.size / Chunk);
    meta = meta || { headers: {}, url: '' };
    var headers = meta.headers || {};
    // Strip any trailing query-string before swapping to /upload-chunked; our
    // chunk URL appends its own params (?session=...&part=N&total=M). We don't
    // forward the original query because Carbonio's upload URL doesn't use one.
    var baseUrl = (meta.url || '').replace(/\?.*$/, '').replace(/\/upload$/, '/upload-chunked');
    var aborted = false, failed = false, finalising = false;
    var partLoaded = new Array(total).fill(0);
    var lastBody = '', lastStatus = 0, lastCT = null;
    var lastDispatchAt = 0, pendingDispatch = null;
    var finalFake = 0, finaliseTimer = null;

    if (!sessions[sid]) sessionStart(sid, file.name, file.size);
    var inflightParts = [];
    xhr.abort = function () {
      aborted = true;
      // Also abort any in-flight chunk XHRs so we stop uploading bytes once
      // the caller cancels. Native XHR.abort() may throw if the request is
      // already complete — wrap each call.
      for (var ap = 0; ap < inflightParts.length; ap++) {
        try { inflightParts[ap].abort(); } catch (e) {}
      }
      inflightParts = [];
    };

    function dispatch() {
      pendingDispatch = null;
      lastDispatchAt = Date.now();
      var loaded = 0;
      for (var i = 0; i < partLoaded.length; i++) loaded += partLoaded[i];
      var uploadPart = Math.min(loaded, file.size) * UploadPhaseRatio;
      var finalPart = finalFake * (1 - UploadPhaseRatio);
      var combined = Math.min(uploadPart + finalPart, file.size);
      var evt;
      try { evt = new ProgressEvent('progress', { lengthComputable: true, loaded: combined, total: file.size }); }
      catch (e) { evt = new Event('progress'); evt.loaded = combined; evt.total = file.size; evt.lengthComputable = true; }
      xhr.upload.dispatchEvent(evt);
      sessionProgress(sid, combined, finalising ? 'merging' : '');
    }
    function scheduleDispatch() {
      var now = Date.now();
      var elapsed = now - lastDispatchAt;
      if (elapsed >= ProgressThrottleMs) { dispatch(); return; }
      if (pendingDispatch) return;
      pendingDispatch = setTimeout(dispatch, ProgressThrottleMs - elapsed);
    }

    function uploadOne(idx) {
      return new Promise(function (resolve, reject) {
        if (aborted) { reject(new Error('aborted')); return; }
        var slice = file.slice(idx * Chunk, Math.min((idx + 1) * Chunk, file.size));
        // Use the *original* XHR constructor for chunks — re-entering our own
        // wrapper would re-detect the URL etc.
        var part = new OrigXHR();
        inflightParts.push(part);
        function dropPart() {
          var pi = inflightParts.indexOf(part);
          if (pi !== -1) inflightParts.splice(pi, 1);
        }
        part.open('POST',
          baseUrl + '?session=' + sid + '&part=' + idx + '&total=' + total, true);
        for (var k in headers) {
          if (k.toLowerCase() !== 'content-length') part.setRequestHeader(k, headers[k]);
        }
        part.upload.addEventListener('progress', function (e) {
          partLoaded[idx] = e.loaded; scheduleDispatch();
        });
        part.addEventListener('load', function () {
          dropPart();
          if (part.status >= 200 && part.status < 300) {
            partLoaded[idx] = slice.size;
            scheduleDispatch();
            // The sidecar replies the final Java response on whichever chunk
            // happened to be the LAST one received (not necessarily idx ===
            // total-1, since chunks fly in parallel). Detect by content:
            // Java's reply contains "nodeId", intermediate "received" doesn't.
            var rt = part.responseText || '';
            if (rt.indexOf('"nodeId"') !== -1 || rt.indexOf('"node"') !== -1) {
              lastBody = rt;
              lastStatus = part.status;
              lastCT = part.getResponseHeader('Content-Type');
            }
            resolve();
          } else {
            if (!failed) {
              failed = true;
              lastBody = part.responseText;
              lastStatus = part.status;
              lastCT = part.getResponseHeader('Content-Type');
            }
            reject(new Error('part ' + idx + ' http ' + part.status));
          }
        });
        part.addEventListener('error', function () { dropPart(); reject(new Error('part ' + idx + ' network')); });
        part.addEventListener('abort', function () { dropPart(); reject(new Error('part ' + idx + ' aborted')); });
        part.send(slice);
      });
    }

    function startFinalisation() {
      if (finalising) return;
      finalising = true;
      var maxFake = file.size * 0.99;
      finaliseTimer = setInterval(function () {
        if (!finalising) { clearInterval(finaliseTimer); return; }
        if (finalFake < maxFake) {
          var remaining = maxFake - finalFake;
          finalFake += Math.max(remaining * 0.02, file.size * 0.001);
          scheduleDispatch();
        }
      }, FinalisingPollMs);
    }
    function stopFinalisation() {
      finalising = false;
      if (finaliseTimer) { clearInterval(finaliseTimer); finaliseTimer = null; }
    }

    function complete(status, body, ct) {
      // Stop the synthetic finalisation interval FIRST so it can't race past
      // our final 100% dispatch and overwrite the bar.
      stopFinalisation();
      // Also cancel any pending throttled dispatch (might fire after we set
      // the final progress and Carbonio's listener would batch 95% as last).
      if (pendingDispatch) { clearTimeout(pendingDispatch); pendingDispatch = null; }
      // Sentinel values: dispatch() does Math.min(loaded * 0.95 + finalFake *
      // 0.05, file.size). Setting partLoaded high enough that the sum
      // saturates to file.size means any straggling dispatch() will report
      // 100%, not a stale lower value.
      finalFake = file.size * 100;
      for (var pp = 0; pp < partLoaded.length; pp++) partLoaded[pp] = (file.size / total) * 2;
      try { Object.defineProperty(xhr, 'status', { configurable: true, get: function () { return status; } }); } catch (e) {}
      try { Object.defineProperty(xhr, 'statusText', { configurable: true, get: function () { return status === 200 ? 'OK' : 'Error'; } }); } catch (e) {}
      try { Object.defineProperty(xhr, 'responseText', { configurable: true, get: function () { return body || ''; } }); } catch (e) {}
      try { Object.defineProperty(xhr, 'response', { configurable: true, get: function () { return body || ''; } }); } catch (e) {}
      try { Object.defineProperty(xhr, 'readyState', { configurable: true, get: function () { return 4; } }); } catch (e) {}
      xhr.getResponseHeader = function (n) { return (n || '').toLowerCase() === 'content-type' ? ct : null; };
      xhr.getAllResponseHeaders = function () { return ct ? 'content-type: ' + ct + '\r\n' : ''; };
      try {
        var evt = new ProgressEvent('progress', { lengthComputable: true, loaded: file.size, total: file.size });
        xhr.upload.dispatchEvent(evt);
      } catch (e) {}
      xhr.dispatchEvent(new Event('readystatechange'));
      xhr.dispatchEvent(new Event('load'));
      xhr.dispatchEvent(new Event('loadend'));
      var ok = status >= 200 && status < 300;
      sessionEnd(sid, ok);
      if (ok) {
        queueFolderRefresh(headers.ParentId || headers.parentid || headers.parentId);
      }
    }

    function fail(err) {
      stopFinalisation();
      aborted = true;
      try { Object.defineProperty(xhr, 'status', { configurable: true, get: function () { return lastStatus || 0; } }); } catch (e) {}
      try { Object.defineProperty(xhr, 'responseText', { configurable: true, get: function () { return lastBody || ''; } }); } catch (e) {}
      try { Object.defineProperty(xhr, 'readyState', { configurable: true, get: function () { return 4; } }); } catch (e) {}
      xhr.dispatchEvent(new Event('error'));
      xhr.dispatchEvent(new Event('loadend'));
      sessionEnd(sid, false);
    }

    sessionAcquire().then(function () {
      if (aborted) { sessionRelease(); fail(new Error('aborted before start')); return; }
      sessionActive(sid);
      var nextIdx = 0;
      function spawn() {
        if (failed || aborted) return Promise.resolve();
        if (nextIdx >= total) return Promise.resolve();
        var idx = nextIdx++;
        return uploadOne(idx).then(spawn);
      }
      var workerCount = Math.min(calcWorkers(sid), total);
      var workers = [];
      for (var i = 0; i < workerCount; i++) workers.push(spawn());
      var watcher = setInterval(function () {
        if (finalising || failed || aborted) { clearInterval(watcher); return; }
        var doneParts = 0;
        for (var k = 0; k < partLoaded.length; k++) {
          if (partLoaded[k] >= file.size / total * 0.99) doneParts++;
        }
        if (doneParts >= total - 1) { startFinalisation(); clearInterval(watcher); }
      }, 1000);
      Promise.all(workers).then(function () {
        if (failed || aborted) fail(new Error('one or more parts failed'));
        else complete(lastStatus || 200, lastBody, lastCT);
      }).catch(fail).then(function () { sessionRelease(); });
    });
  }
})();
