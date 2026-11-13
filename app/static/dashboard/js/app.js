/* ============================================
   NightJar — app controller
   ============================================ */

let _appData = null;
let _currentPageId = 'overview';
let _currentTimeRange = '7d';
let _connected = false;

const PAGE_TITLES = {
  overview: 'Overview',
  requests: 'Requests',
  cache: 'Cache',
  router: 'Router',
  telemetry: 'Telemetry',
};

// ─── NAVIGATION ──────────────────────────────────────────────
function navigateTo(pageId) {
  document.querySelectorAll('.nav-item').forEach(el => {
    const active = el.dataset.page === pageId;
    el.classList.toggle('active', active);
    if (active) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });

  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
  document.getElementById(`page-${pageId}`)?.classList.add('active');

  const headerTitle = document.getElementById('header-title');
  if (headerTitle) headerTitle.textContent = PAGE_TITLES[pageId] || pageId;
  document.title = `${PAGE_TITLES[pageId] || pageId} · NightJar`;

  _currentPageId = pageId;

  if (window.innerWidth <= 1024) closeMobileSidebar();

  destroyAllCharts();

  if (!_appData) return;
  switch (pageId) {
    case 'overview': initOverviewPage(_appData); break;
    case 'requests': initRequestsPage(_appData); break;
    case 'cache': initCachePage(_appData); break;
    case 'router': initRouterPage(_appData); break;
    case 'telemetry': initTelemetryPage(_appData); break;
  }
}

// ─── AGGREGATION ─────────────────────────────────────────────
// These turn the list of requests returned by the API into chart data.

function getTimeRangeCutoff(range) {
  const spans = {
    '1h': 3600e3, '6h': 6 * 3600e3, '24h': 24 * 3600e3,
    '7d': 7 * 24 * 3600e3, '30d': 30 * 24 * 3600e3,
  };
  return Date.now() - (spans[range] || spans['7d']);
}

function filterByTimeRange(requests, range) {
  const cutoff = getTimeRangeCutoff(range);
  return requests.filter(r => new Date(r.timestamp).getTime() >= cutoff);
}

// Only produces days that actually have data, so a quiet dashboard
// doesn't imply a fortnight of history it never had.
function aggregateByDay(requests) {
  const byDay = new Map();
  for (const r of requests) {
    const day = (r.timestamp || '').slice(0, 10);
    if (!day) continue;
    if (!byDay.has(day)) {
      byDay.set(day, { date: day, total_requests: 0, cache_hits: 0, cache_misses: 0, _latency: 0, total_cost: 0 });
    }
    const b = byDay.get(day);
    const hit = r.cacheHit != null ? r.cacheHit : r.cache_hit;
    b.total_requests += 1;
    if (hit) b.cache_hits += 1; else b.cache_misses += 1;
    b._latency += r.latency != null ? r.latency : (r.latency_ms || 0);
    b.total_cost += r.cost != null ? r.cost : (r.estimated_cost_usd || 0);
  }
  return [...byDay.values()]
    .sort((a, b) => a.date.localeCompare(b.date))
    .map(b => ({
      date: b.date,
      total_requests: b.total_requests,
      cache_hits: b.cache_hits,
      cache_misses: b.cache_misses,
      avg_latency_ms: b.total_requests ? Math.round(b._latency / b.total_requests) : 0,
      total_cost: +b.total_cost.toFixed(6),
    }));
}

function aggregateByReason(requests) {
  const byReason = new Map();
  for (const r of requests) {
    const reason = r.routing_reason;
    if (!reason) continue;
    if (!byReason.has(reason)) {
      byReason.set(reason, { reason, tier: r.tier || 'unknown', count: 0, _latency: 0 });
    }
    const b = byReason.get(reason);
    b.count += 1;
    b._latency += r.latency != null ? r.latency : (r.latency_ms || 0);
  }
  return [...byReason.values()]
    .map(b => ({ ...b, avgLatency: Math.round(b._latency / b.count) }))
    .sort((a, b) => b.count - a.count);
}

function tierCounts(requests) {
  return {
    simple: requests.filter(r => r.tier === 'simple').length,
    complex: requests.filter(r => r.tier === 'complex').length,
  };
}

function similarityScoresFromHits(requests) {
  return requests
    .filter(r => (r.cacheHit != null ? r.cacheHit : r.cache_hit))
    .map(r => (r.similarity_score != null ? r.similarity_score : r.similarity))
    .filter(s => s != null);
}

// ─── PAGES ───────────────────────────────────────────────────
// "Nothing here yet" and "nothing in the last hour" are different
// situations and deserve different wording.
function emptyReason(data) {
  return data.requests.length === 0
    ? 'No requests yet — send one through /v1/chat/completions.'
    : 'No requests in this time range.';
}

function initOverviewPage(data) {
  const inRange = filterByTimeRange(data.requests, _currentTimeRange);
  const s = data.summary;

  setText('metric-total-requests', s.total_requests.toLocaleString());
  setText('metric-hit-rate', formatPercent(s.cache_hit_rate));
  setText('metric-avg-latency', formatLatency(s.avg_latency_ms));
  setText('metric-total-cost', formatCost(s.total_cost));

  const empty = emptyReason(data);
  initRequestsChart(aggregateByDay(inRange), empty);
  initModelsChart(inRange, empty);
  renderRecentRequestsTable(data.requests);
}

function initRequestsPage(data) {
  populateModelFilter(data.requests);
  renderRequestsTable(data.requests);
}

function initCachePage(data) {
  const scores = similarityScoresFromHits(data.requests);
  const avg = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : 0;
  const c = data.cacheStats;

  setText('cache-total-entries', (c.total_vectors ?? 0).toLocaleString());
  setText('cache-redis-keys', (c.redis_keys ?? 0).toLocaleString());
  setText('cache-hit-rate', formatPercent(data.summary.cache_hit_rate));
  setText('cache-threshold', `${((c.similarity_threshold ?? 0) * 100).toFixed(0)}%`);
  setText('cache-avg-sim', scores.length ? `${(avg * 100).toFixed(1)}%` : '—');

  initSimilarityChart(scores);
  renderCacheHitsTable(data.requests);
}

function initRouterPage(data) {
  const { simple, complex } = tierCounts(data.requests);
  const total = simple + complex;

  drawRing(total ? (complex / total) * 100 : 0);
  setText('router-total-requests', total.toLocaleString());
  setText('router-simple-count', simple.toLocaleString());
  setText('router-complex-count', complex.toLocaleString());

  initTierChart(simple, complex);
  renderReasonTable(aggregateByReason(data.requests));
}

function initTelemetryPage(data) {
  const daily = aggregateByDay(filterByTimeRange(data.requests, _currentTimeRange));
  const empty = emptyReason(data);
  initCostChart(daily, empty);
  initLatencyChart(daily, empty);
  initHitRateChart(daily, empty);
}

function handleClearCache() {
  if (!_connected) {
    showToast("Can't clear the cache while the backend is unreachable", 'error');
    return;
  }
  showModal('confirm-modal');

  const confirmBtn = document.getElementById('confirm-action');
  if (!confirmBtn) return;
  confirmBtn.onclick = async () => {
    setButtonLoading(confirmBtn, 'Clearing…');
    try {
      const resp = await apiFetch('/v1/cache', { method: 'DELETE' });
      const data = await resp.json();
      hideModal('confirm-modal');
      showToast(
        `Cache cleared — ${data.vectors_cleared} vector${data.vectors_cleared === 1 ? '' : 's'} and ${data.redis_keys_cleared} stored response${data.redis_keys_cleared === 1 ? '' : 's'} removed`,
        'success',
      );
      await loadData();
      navigateTo(_currentPageId);
    } catch {
      hideModal('confirm-modal');
      showToast("Couldn't clear the cache — the backend didn't respond", 'error');
    } finally {
      resetButton(confirmBtn);
    }
  };
}

function handleExportCSV() {
  if (!_appData) return;
  const rows = getFilteredRequests(_appData.requests);
  if (!rows.length) {
    showToast('Nothing to export with the current filters', 'info');
    return;
  }

  const headers = ['Request ID', 'Timestamp', 'Model', 'Tier', 'Routing reason',
                   'Prompt tokens', 'Completion tokens', 'Latency (ms)', 'Cost (USD)', 'Cache', 'Similarity'];
  const esc = v => {
    const s = String(v ?? '');
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const body = rows.map(r => [
    r.id, r.timestamp, r.model, r.tier || '', r.routing_reason || '',
    r.tokens_in, r.tokens_out, r.latency_ms, r.cost, r.cache_status, r.similarity_score ?? '',
  ].map(esc).join(','));

  const csv = [headers.join(','), ...body].join('\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `nightjar-requests-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);

  showToast(`Exported ${rows.length} request${rows.length === 1 ? '' : 's'}`, 'success');
}

function handleTimeRange(range) {
  _currentTimeRange = range;
  document.querySelectorAll('.time-range-btn').forEach(b => {
    const active = b.dataset.range === range;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', String(active));
  });
  if (_appData && (_currentPageId === 'overview' || _currentPageId === 'telemetry')) {
    navigateTo(_currentPageId);
  }
}

// ─── API ─────────────────────────────────────────────────────
function getApiBaseUrl() {
  // Served from the app at /dashboard/ → same origin. Opened as a file
  // → assume the default local port.
  if (window.location.pathname.startsWith('/dashboard')) return window.location.origin;
  return 'http://localhost:8000';
}

// Only needed when the backend has NIGHTJAR_API_KEYS set (auth is off
// by default). Set one from the console:
//   localStorage.setItem('nightjar_api_key', 'your-token')
function getApiHeaders() {
  let token = null;
  try { token = localStorage.getItem('nightjar_api_key') || localStorage.getItem('contextforge_api_key'); } catch { token = null; }
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function apiFetch(path, options = {}) {
  const resp = await fetch(`${getApiBaseUrl()}${path}`, {
    ...options,
    headers: { ...getApiHeaders(), ...(options.headers || {}) },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp;
}

function normalizeApiRecord(r) {
  return {
    id: r.request_id, request_id: r.request_id, timestamp: r.timestamp,
    model: r.model_used || r.model_requested, model_used: r.model_used,
    tokens_in: r.prompt_tokens || 0, tokens_out: r.completion_tokens || 0,
    latency_ms: r.latency_ms || 0, cost: r.estimated_cost_usd || 0,
    http_status: r.http_status ?? null, error_code: r.error_code || null, cache_tier: r.cache_tier || null,
    cache_status: (r.http_status >= 400) ? 'ERROR' : (r.cache_hit ? 'HIT' : 'MISS'), cache_hit: !!r.cache_hit,
    similarity_score: r.similarity_score,
    compressed: r.compressed || false, compression_ratio: r.compression_ratio || 1.0,
    tier: r.tier || null, routing_reason: r.routing_reason || null,
  };
}

function normalizeApiSummary(s) {
  return {
    total_requests: s.total_requests || 0,
    cache_hit_rate: s.cache_hit_rate != null ? +(s.cache_hit_rate * 100).toFixed(1) : 0,
    avg_latency_ms: s.avg_latency_ms || 0,
    total_cost: s.total_cost_usd || 0,
    cache_hits: s.cache_hits || 0,
  };
}

async function loadData() {
  try {
    _connected = await checkAPIConnection();
  } catch {
    _connected = false;
  }
  updateConnectionStatus(_connected);

  if (_connected) {
    try {
      const [summaryRes, requestsRes, cacheRes] = await Promise.all([
        apiFetch('/v1/telemetry/summary'),
        apiFetch('/v1/telemetry?limit=500'),
        apiFetch('/v1/cache/stats'),
      ]);
      const [summary, requests, cacheStats] = await Promise.all([
        summaryRes.json(), requestsRes.json(), cacheRes.json(),
      ]);

      _appData = {
        summary: normalizeApiSummary(summary),
        requests: (requests.records || []).map(normalizeApiRecord),
        cacheStats,
      };
      return;
    } catch {
      // Reachable but not answering properly — say so rather than
      // quietly swapping in demo numbers that look real.
      _connected = false;
      updateConnectionStatus(false);
      showToast("Backend reachable but didn't return data", 'warning');
    }
  }
  // No backend, no data. There is deliberately no sample dataset: an invented request log that
  // looks real is worse than an empty page.
  _appData = {
    summary: normalizeApiSummary({}),
    requests: [],
    cacheStats: {},
  };
}

// ─── HELPERS ─────────────────────────────────────────────────
function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function drawRing(pct) {
  const circle = document.getElementById('accuracy-ring-fill');
  const label = document.getElementById('accuracy-ring-label');
  if (!circle) return;

  const circumference = 2 * Math.PI * 56;
  circle.style.strokeDasharray = String(circumference);
  circle.style.strokeDashoffset = String(circumference - (pct / 100) * circumference);
  if (label) label.textContent = `${Math.round(pct)}%`;
}

// ─── LIVE REFRESH ────────────────────────────────────────────
let _liveTimer = null;
const REFRESH_MS = 10000;

function toggleLiveRefresh() {
  const btn = document.getElementById('live-refresh-btn');
  const on = !_liveTimer;

  if (on) {
    _liveTimer = setInterval(async () => {
      await loadData();
      navigateTo(_currentPageId);
    }, REFRESH_MS);
    showToast('Refreshing every 10 seconds', 'info', 2500);
  } else {
    clearInterval(_liveTimer);
    _liveTimer = null;
    showToast('Auto-refresh off', 'info', 2000);
  }

  btn?.classList.toggle('active', on);
  btn?.setAttribute('aria-pressed', String(on));
}

// ─── EVENTS ──────────────────────────────────────────────────
function bindEvents() {
  document.querySelectorAll('.nav-item[data-page]').forEach(item => {
    item.addEventListener('click', () => navigateTo(item.dataset.page));
  });

  document.querySelectorAll('.time-range-btn[data-range]').forEach(btn => {
    btn.addEventListener('click', () => handleTimeRange(btn.dataset.range));
  });

  document.addEventListener('click', e => {
    const copyBtn = e.target.closest('[data-copy]');
    if (copyBtn) {
      copyToClipboard(copyBtn.dataset.copy, copyBtn);
      return;
    }

    const actionEl = e.target.closest('[data-action]');
    if (!actionEl) return;
    switch (actionEl.dataset.action) {
      case 'toggle-sidebar': toggleSidebar(); break;
      case 'goto-requests': navigateTo('requests'); break;
      case 'export-csv': handleExportCSV(); break;
      case 'clear-cache': handleClearCache(); break;
      case 'toggle-live-refresh': toggleLiveRefresh(); break;
      case 'close-modal':
      case 'cancel-modal': hideModal(actionEl.closest('.modal-overlay')?.id); break;
    }
  });

  // Escape closes the dialog — expected of anything modal.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('.modal-overlay.active').forEach(m => hideModal(m.id));
  });

  // Clicking the backdrop dismisses too.
  document.getElementById('confirm-modal')?.addEventListener('click', e => {
    if (e.target.id === 'confirm-modal') hideModal('confirm-modal');
  });

  const rerender = () => { _currentPage = 1; renderRequestsTable(_appData.requests); };
  const search = document.getElementById('req-search');
  if (search) {
    search.addEventListener('input', e => { _searchTerm = e.target.value; rerender(); });
  }
  [['filter-model', v => (_filterModel = v)],
   ['filter-status', v => (_filterStatus = v)],
   ['tier-filter', v => (_filterTier = v)]].forEach(([id, set]) => {
    document.getElementById(id)?.addEventListener('change', e => { set(e.target.value); rerender(); });
  });

  document.getElementById('prev-page')?.addEventListener('click', () => prevPage(_appData.requests));
  document.getElementById('next-page')?.addEventListener('click', () => nextPage(_appData.requests));
}

// ─── BOOT ────────────────────────────────────────────────────
async function init() {
  initSidebar();
  bindEvents();
  await loadData();
  navigateTo('overview');
  hideBoot();
}

document.addEventListener('DOMContentLoaded', init);
