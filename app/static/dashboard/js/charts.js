/* ============================================
   NightJar — charts
   ============================================ */

const _charts = {};

// Pulled from the CSS custom properties so the charts can't drift
// from the rest of the palette.
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

const PALETTE = [
  '#2563eb', '#7c5cd6', '#0e9f6e', '#d98324',
  '#3f8ecc', '#a05fc4', '#5d7085', '#c2557a',
];

function _applyDefaults() {
  const grid = cssVar('--chart-grid', '#edeff3');
  Chart.defaults.color = cssVar('--text-tertiary', '#858d99');
  Chart.defaults.borderColor = grid;
  Chart.defaults.font.family = "'Inter', -apple-system, BlinkMacSystemFont, sans-serif";
  Chart.defaults.font.size = 11;
  Chart.defaults.plugins.legend.labels.boxWidth = 8;
  Chart.defaults.plugins.legend.labels.boxHeight = 8;
  Chart.defaults.plugins.legend.labels.padding = 14;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.pointStyle = 'circle';
  Chart.defaults.animation = prefersReducedMotion()
    ? false
    : { duration: 400, easing: 'easeOutQuart' };
  Chart.defaults.plugins.tooltip.backgroundColor = '#171a1f';
  Chart.defaults.plugins.tooltip.titleColor = '#ffffff';
  Chart.defaults.plugins.tooltip.bodyColor = '#d9dde3';
  Chart.defaults.plugins.tooltip.borderWidth = 0;
  Chart.defaults.plugins.tooltip.cornerRadius = 6;
  Chart.defaults.plugins.tooltip.padding = 9;
  Chart.defaults.plugins.tooltip.boxPadding = 4;
  Chart.defaults.plugins.tooltip.displayColors = true;
}

function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// With only a day or two of data a bar chart otherwise renders one
// enormous slab across the card, which reads as a rendering bug.
const MAX_BAR = 48;

// Draws a short message inside the canvas area instead of leaving an
// empty grid, so a quiet dashboard doesn't look broken.
function _renderChartEmpty(canvasId, message) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return true;
  const wrapper = canvas.parentElement;
  let note = wrapper.querySelector('.chart-empty');
  if (!note) {
    note = document.createElement('div');
    note.className = 'chart-empty empty-state';
    wrapper.appendChild(note);
  }
  note.innerHTML = `<div class="empty-state-text">${message}</div>`;
  note.hidden = false;
  canvas.style.visibility = 'hidden';
  return true;
}

function _clearChartEmpty(canvasId) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const note = canvas.parentElement.querySelector('.chart-empty');
  if (note) note.hidden = true;
  canvas.style.visibility = 'visible';
}

const GRID = { color: '#edeff3', drawTicks: false };

// ─── REQUESTS OVER TIME ──────────────────────────────────────
function initRequestsChart(data, emptyMsg = 'No requests in this time range.') {
  _applyDefaults();
  const ctx = document.getElementById('chart-requests');
  if (!ctx) return;
  if (!data.length) return _renderChartEmpty('chart-requests', emptyMsg);
  _clearChartEmpty('chart-requests');

  _charts.requests = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: data.map(d => formatDayLabel(d.date)),
      datasets: [
        {
          label: 'Served from cache',
          data: data.map(d => d.cache_hits),
          backgroundColor: '#0e9f6e',
          borderRadius: 3,
          maxBarThickness: MAX_BAR,
        },
        {
          label: 'Upstream calls',
          data: data.map(d => d.cache_misses),
          backgroundColor: '#93b4fb',
          borderRadius: 3,
          maxBarThickness: MAX_BAR,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'bottom', align: 'start' } },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 0 } },
        y: { stacked: true, grid: GRID, border: { display: false }, ticks: { precision: 0 } },
      },
    },
  });
}

// ─── MODELS ──────────────────────────────────────────────────
function initModelsChart(data, emptyMsg = 'No requests in this time range.') {
  _applyDefaults();
  const ctx = document.getElementById('chart-models');
  if (!ctx) return;

  const counts = {};
  data.forEach(r => {
    const m = r.model || r.model_used;
    if (m) counts[m] = (counts[m] || 0) + 1;
  });
  const labels = Object.keys(counts);
  if (!labels.length) return _renderChartEmpty('chart-models', emptyMsg);
  _clearChartEmpty('chart-models');

  _charts.models = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data: Object.values(counts),
        backgroundColor: PALETTE.slice(0, labels.length),
        borderWidth: 2,
        borderColor: '#ffffff',
        hoverOffset: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '66%',
      plugins: { legend: { position: 'right' } },
    },
  });
}

// ─── SIMILARITY DISTRIBUTION ─────────────────────────────────
// `scores` is a plain array of similarity values from recent cache
// hits — real numbers, not a per-entry cache dump (the backend never
// exposes cached prompt text).
function initSimilarityChart(scores) {
  _applyDefaults();
  const ctx = document.getElementById('chart-similarity');
  if (!ctx) return;
  if (!scores.length) {
    return _renderChartEmpty('chart-similarity',
      'No cache hits yet. Send a similar prompt twice and it will show up here.');
  }
  _clearChartEmpty('chart-similarity');

  const buckets = { '0.70–0.80': 0, '0.80–0.90': 0, '0.90–0.94': 0, '0.94–0.97': 0, '0.97–1.00': 0 };
  scores.forEach(s => {
    if (s < 0.80) buckets['0.70–0.80']++;
    else if (s < 0.90) buckets['0.80–0.90']++;
    else if (s < 0.94) buckets['0.90–0.94']++;
    else if (s < 0.97) buckets['0.94–0.97']++;
    else buckets['0.97–1.00']++;
  });

  _charts.similarity = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: Object.keys(buckets),
      datasets: [{
        label: 'Cache hits',
        data: Object.values(buckets),
        backgroundColor: '#7c5cd6',
        borderRadius: 3,
        maxBarThickness: MAX_BAR,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false } },
        y: { grid: GRID, border: { display: false }, ticks: { precision: 0 } },
      },
    },
  });
}

// ─── TREND LINES ─────────────────────────────────────────────
function _lineChart(key, canvasId, data, { label, values, color, tickFormat, max, emptyMsg }) {
  _applyDefaults();
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  if (!data.length) return _renderChartEmpty(canvasId, emptyMsg || 'No requests in this time range.');

  // A trend line through one point is a dot in an empty grid — it reads as
  // a broken chart. Report the single value instead and say why there's no
  // line yet.
  if (data.length === 1) {
    return _renderChartEmpty(canvasId,
      `${formatDayLabel(data[0].date)} · <strong>${tickFormat(values[0])}</strong><br>` +
      'A trend line needs more than one day of data.');
  }

  _clearChartEmpty(canvasId);

  _charts[key] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(d => formatDayLabel(d.date)),
      datasets: [{
        label,
        data: values,
        borderColor: color,
        backgroundColor: color + '14',
        borderWidth: 2,
        pointRadius: data.length > 20 ? 0 : 3,
        pointBackgroundColor: color,
        pointBorderColor: '#ffffff',
        pointBorderWidth: 1.5,
        pointHoverRadius: 5,
        fill: true,
        tension: 0.3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { maxRotation: 0 } },
        y: {
          grid: GRID,
          border: { display: false },
          beginAtZero: true,
          max,
          ticks: { callback: tickFormat },
        },
      },
    },
  });
}

function initLatencyChart(data, emptyMsg) {
  _lineChart('latency', 'chart-latency', data, {
    label: 'Avg latency',
    values: data.map(d => d.avg_latency_ms),
    color: '#d98324',
    tickFormat: v => v + 'ms',
    emptyMsg,
  });
}

function initCostChart(data, emptyMsg) {
  _lineChart('cost', 'chart-cost', data, {
    label: 'Spend',
    values: data.map(d => d.total_cost),
    color: '#2563eb',
    tickFormat: v => '$' + Number(v).toFixed(4),
    emptyMsg,
  });
}

function initHitRateChart(data, emptyMsg) {
  _lineChart('hitrate', 'chart-hitrate', data, {
    label: 'Hit rate',
    values: data.map(d => +((d.cache_hits / d.total_requests) * 100).toFixed(1)),
    color: '#0e9f6e',
    tickFormat: v => v + '%',
    max: 100,
    emptyMsg,
  });
}

// ─── TIER SPLIT ──────────────────────────────────────────────
// Deliberately not green/red: a "complex" classification is a routing
// outcome, not a failure.
function initTierChart(simple, complex) {
  _applyDefaults();
  const ctx = document.getElementById('router-models-chart');
  if (!ctx) return;
  if (!simple && !complex) return _renderChartEmpty('router-models-chart', 'No routed requests yet.');
  _clearChartEmpty('router-models-chart');

  _charts.tier = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Simple', 'Complex'],
      datasets: [{
        data: [simple, complex],
        backgroundColor: ['#9aa4b2', '#7c5cd6'],
        borderWidth: 2,
        borderColor: '#ffffff',
        hoverOffset: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '66%',
      plugins: { legend: { position: 'bottom' } },
    },
  });
}

function destroyAllCharts() {
  Object.values(_charts).forEach(c => {
    if (c && typeof c.destroy === 'function') c.destroy();
  });
  Object.keys(_charts).forEach(k => delete _charts[k]);
}
