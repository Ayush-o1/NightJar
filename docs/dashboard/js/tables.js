/* ============================================
   NightJar — table rendering
   ============================================ */

// ─── ESCAPING ────────────────────────────────────────────────
// model/tier/routing_reason originate from request data the *caller*
// controls (the chat completions body, and — for model_used — the
// X-NightJar-Model-Override header), not from the dashboard's own
// state. Everything dynamic is escaped before it goes into innerHTML so
// {"model": "<img src=x onerror=…>"} renders as inert text in the log
// instead of running in whoever is viewing this.
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

// ─── FILTER + PAGING STATE ───────────────────────────────────
let _currentPage = 1;
const _pageSize = 12;
let _searchTerm = '';
let _filterModel = '';
let _filterStatus = '';
let _filterTier = '';

function _field(r, ...names) {
  for (const n of names) {
    if (r[n] !== undefined && r[n] !== null) return r[n];
  }
  return undefined;
}

function _isHit(r) {
  return r.cacheHit != null ? r.cacheHit : r.cache_hit;
}

function getFilteredRequests(requests) {
  return requests.filter(r => {
    const rid = _field(r, 'id', 'request_id') || '';
    const rmodel = _field(r, 'model', 'model_used') || '';
    const cacheStatus = r.cache_status || (_isHit(r) ? 'HIT' : 'MISS');

    if (_searchTerm) {
      const q = _searchTerm.toLowerCase();
      if (!rid.toLowerCase().includes(q) && !rmodel.toLowerCase().includes(q)) return false;
    }
    if (_filterModel && rmodel !== _filterModel) return false;
    if (_filterStatus && cacheStatus !== _filterStatus) return false;
    if (_filterTier && r.tier !== _filterTier) return false;
    return true;
  });
}

function hasActiveFilters() {
  return Boolean(_searchTerm || _filterModel || _filterStatus || _filterTier);
}

// ─── SHARED CELL RENDERERS ───────────────────────────────────
function _emptyRow(colspan, title, text) {
  return `<tr><td colspan="${colspan}">
    <div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <div class="empty-state-title">${title}</div>
      <div class="empty-state-text">${text}</div>
    </div>
  </td></tr>`;
}

function _latencyClass(ms) {
  // Most requests are fine, so most stay neutral. Colour is reserved
  // for the ones actually worth a second look.
  return ms < 800 ? 'latency-fast' : ms < 2000 ? 'latency-medium' : 'latency-slow';
}

function _tierPill(tier) {
  if (!tier) return '<span class="text-muted">—</span>';
  const cls = tier === 'complex' ? 'tier-complex' : 'tier-simple';
  return `<span class="pill ${cls}">${escapeHtml(tier)}</span>`;
}

function _cachePill(r) {
  const hit = _isHit(r);
  const sim = _field(r, 'similarity_score', 'similarity');
  const pill = `<span class="pill ${hit ? 'hit' : 'miss'}">${hit ? 'HIT' : 'MISS'}</span>`;
  if (hit && sim != null) {
    return `${pill}<span class="sim-score">${(sim * 100).toFixed(0)}%</span>`;
  }
  return pill;
}

function _idCell(rid) {
  return `<span class="mono">${escapeHtml(rid.slice(0, 12))}</span>
    <button type="button" class="copy-btn" data-copy="${escapeHtml(rid)}" aria-label="Copy request ID ${escapeHtml(rid)}" title="Copy ID">⧉</button>`;
}

// ─── REQUESTS TABLE ──────────────────────────────────────────
function renderRequestsTable(requests) {
  const tbody = document.getElementById('req-tbody');
  if (!tbody) return;

  const filtered = getFilteredRequests(requests);
  const totalPages = Math.max(1, Math.ceil(filtered.length / _pageSize));
  if (_currentPage > totalPages) _currentPage = totalPages;

  const start = (_currentPage - 1) * _pageSize;
  const pageItems = filtered.slice(start, start + _pageSize);

  if (!pageItems.length) {
    tbody.innerHTML = hasActiveFilters()
      ? _emptyRow(8, 'No matching requests', 'Try clearing a filter or searching for a different model.')
      : _emptyRow(8, 'No requests yet',
          'Point a client at <code>/v1/chat/completions</code> and requests will appear here.');
  } else {
    tbody.innerHTML = pageItems.map(r => {
      const rid = _field(r, 'id', 'request_id') || '';
      const rmodel = _field(r, 'model', 'model_used') || '';
      const lat = Number(_field(r, 'latency', 'latency_ms') || 0);
      const cost = Number(_field(r, 'cost', 'estimated_cost_usd') || 0);
      const hit = _isHit(r);
      const tIn = _field(r, 'tokens_in', 'prompt_tokens') || 0;
      const tOut = _field(r, 'tokens_out', 'completion_tokens') || 0;
      // A cache hit consumed no upstream tokens — "0 / 0" reads like a
      // measurement, a dash reads like "not applicable", which is true.
      const tokens = hit ? '<span class="text-muted">—</span>'
                         : `${tIn.toLocaleString()} / ${tOut.toLocaleString()}`;

      return `<tr>
        <td data-label="Request ID">${_idCell(rid)}</td>
        <td data-label="Time">${timeAgo(r.timestamp)}</td>
        <td data-label="Model">${escapeHtml(rmodel)}</td>
        <td data-label="Tier">${_tierPill(r.tier)}</td>
        <td data-label="Tokens">${tokens}</td>
        <td data-label="Latency" class="${_latencyClass(lat)}">${formatLatency(lat)}</td>
        <td data-label="Cost">${formatCost(cost)}</td>
        <td data-label="Cache">${_cachePill(r)}</td>
      </tr>`;
    }).join('');
  }

  const info = document.getElementById('page-info');
  if (info) {
    const end = Math.min(start + _pageSize, filtered.length);
    info.textContent = filtered.length === 0
      ? 'Nothing to show'
      : `${start + 1}–${end} of ${filtered.length}`;
  }

  const prevBtn = document.getElementById('prev-page');
  const nextBtn = document.getElementById('next-page');
  if (prevBtn) prevBtn.disabled = _currentPage <= 1;
  if (nextBtn) nextBtn.disabled = _currentPage >= totalPages;
}

function prevPage(requests) {
  if (_currentPage > 1) { _currentPage--; renderRequestsTable(requests); }
}

function nextPage(requests) {
  const totalPages = Math.ceil(getFilteredRequests(requests).length / _pageSize);
  if (_currentPage < totalPages) { _currentPage++; renderRequestsTable(requests); }
}

function resetFilters() {
  _searchTerm = _filterModel = _filterStatus = _filterTier = '';
  _currentPage = 1;
  ['req-search', 'filter-model', 'filter-status', 'tier-filter'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
}

// Built from the models actually present in the data rather than a
// hardcoded list, which would drift from whatever is configured.
function populateModelFilter(requests) {
  const select = document.getElementById('filter-model');
  if (!select) return;
  const models = [...new Set(requests.map(r => _field(r, 'model', 'model_used')).filter(Boolean))].sort();
  const current = select.value;
  select.innerHTML = '<option value="">All models</option>' +
    models.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
  if (models.includes(current)) select.value = current;
}

// ─── OVERVIEW: RECENT REQUESTS ───────────────────────────────
function renderRecentRequestsTable(requests) {
  const tbody = document.getElementById('recent-tbody');
  if (!tbody) return;

  const recent = requests.slice(0, 6);
  if (!recent.length) {
    tbody.innerHTML = _emptyRow(6, 'No requests yet',
      'Send one through <code>/v1/chat/completions</code> to see it here.');
    return;
  }

  tbody.innerHTML = recent.map(r => {
    const rid = _field(r, 'id', 'request_id') || '';
    const rmodel = _field(r, 'model', 'model_used') || '';
    const lat = Number(_field(r, 'latency', 'latency_ms') || 0);
    const cost = Number(_field(r, 'cost', 'estimated_cost_usd') || 0);

    return `<tr>
      <td data-label="Request ID"><span class="mono">${escapeHtml(rid.slice(0, 12))}</span></td>
      <td data-label="Time">${timeAgo(r.timestamp)}</td>
      <td data-label="Model">${escapeHtml(rmodel)}</td>
      <td data-label="Latency" class="${_latencyClass(lat)}">${formatLatency(lat)}</td>
      <td data-label="Cost">${formatCost(cost)}</td>
      <td data-label="Cache">${_cachePill(r)}</td>
    </tr>`;
  }).join('');
}

// ─── CACHE: RECENT HITS ──────────────────────────────────────
// The backend keys cached responses by content hash and stores vectors in
// FAISS — never the prompt text — so there is no entry list to browse.
// This shows the real per-request data that does exist.
function renderCacheHitsTable(requests) {
  const tbody = document.getElementById('cache-tbody');
  if (!tbody) return;

  const hits = requests.filter(_isHit).slice(0, 15);
  if (!hits.length) {
    tbody.innerHTML = _emptyRow(4, 'No cache hits yet',
      'Ask the same question twice — or reword it — and the second one should be served from cache.');
    return;
  }

  tbody.innerHTML = hits.map(r => {
    const rid = _field(r, 'id', 'request_id') || '';
    const rmodel = _field(r, 'model', 'model_used') || '';
    const sim = _field(r, 'similarity_score', 'similarity');
    return `<tr>
      <td data-label="Request ID"><span class="mono">${escapeHtml(rid.slice(0, 12))}</span></td>
      <td data-label="Model">${escapeHtml(rmodel)}</td>
      <td data-label="Similarity">${sim != null ? (sim * 100).toFixed(1) + '%' : '—'}</td>
      <td data-label="Time">${timeAgo(r.timestamp)}</td>
    </tr>`;
  }).join('');
}

// ─── ROUTER: REASONS ─────────────────────────────────────────
// Grouped by the router's actual decision reason (see ModelRouter._classify)
// rather than an invented topic taxonomy.
function renderReasonTable(rows) {
  const tbody = document.getElementById('router-tbody');
  if (!tbody) return;

  if (!rows.length) {
    tbody.innerHTML = _emptyRow(4, 'Nothing routed yet',
      'Routing decisions are recorded per request and grouped here.');
    return;
  }

  tbody.innerHTML = rows.map(row => `<tr>
    <td data-label="Reason"><span class="mono">${escapeHtml(row.reason)}</span></td>
    <td data-label="Tier">${_tierPill(row.tier)}</td>
    <td data-label="Requests">${row.count.toLocaleString()}</td>
    <td data-label="Avg latency" class="${_latencyClass(row.avgLatency)}">${formatLatency(row.avgLatency)}</td>
  </tr>`).join('');
}

// ─── THRESHOLD: HISTORY ──────────────────────────────────────
function renderThresholdHistory(rows) {
  const tbody = document.getElementById('threshold-tbody');
  if (!tbody) return;

  if (!rows.length) {
    tbody.innerHTML = _emptyRow(3, 'No evaluations yet',
      'Run one to record how the threshold responds to the current hit rate.');
    return;
  }

  tbody.innerHTML = rows.map(r => `<tr>
    <td data-label="When">${timeAgo(r.evaluated_at)}</td>
    <td data-label="Threshold">${(r.threshold * 100).toFixed(1)}%</td>
    <td data-label="Hit rate at the time">${(r.cache_hit_rate * 100).toFixed(1)}%</td>
  </tr>`).join('');
}
