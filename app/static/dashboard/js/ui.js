/* ============================================
   NightJar — UI utilities
   ============================================ */

// ─── TOASTS ──────────────────────────────────────────────────
function showToast(message, type = 'success', duration = 4000) {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;

  const icons = { success: '✓', error: '!', warning: '!', info: 'i' };
  const icon = document.createElement('span');
  icon.className = 'toast-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = icons[type] || icons.info;

  const text = document.createElement('span');
  text.textContent = message;

  toast.append(icon, text);
  toast.addEventListener('click', () => dismissToast(toast));
  container.appendChild(toast);

  setTimeout(() => dismissToast(toast), duration);
  return toast;
}

function dismissToast(toastEl) {
  if (!toastEl || toastEl.classList.contains('removing')) return;
  toastEl.classList.add('removing');
  setTimeout(() => toastEl.remove(), 200);
}

// ─── MODAL ───────────────────────────────────────────────────
let _lastFocused = null;

function showModal(modalId) {
  const modal = document.getElementById(modalId);
  if (!modal) return;
  _lastFocused = document.activeElement;
  modal.classList.add('active');
  // Send focus somewhere useful rather than leaving it behind the dialog.
  const target = modal.querySelector('.btn-danger, .btn-primary, button');
  if (target) target.focus();
}

function hideModal(modalId) {
  const modal = document.getElementById(modalId);
  if (!modal) return;
  modal.classList.remove('active');
  if (_lastFocused && typeof _lastFocused.focus === 'function') _lastFocused.focus();
  _lastFocused = null;
}

// ─── SIDEBAR ─────────────────────────────────────────────────
function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('mobile-overlay');

  if (window.innerWidth <= 1024) {
    const open = sidebar.classList.toggle('mobile-open');
    overlay.classList.toggle('active', open);
  } else {
    sidebar.classList.toggle('collapsed');
  }
}

function closeMobileSidebar() {
  document.getElementById('sidebar')?.classList.remove('mobile-open');
  document.getElementById('mobile-overlay')?.classList.remove('active');
}

function initSidebar() {
  document.getElementById('mobile-overlay')?.addEventListener('click', closeMobileSidebar);
}

// ─── BUTTON STATES ───────────────────────────────────────────
function setButtonLoading(buttonEl, text) {
  buttonEl._originalHTML = buttonEl.innerHTML;
  buttonEl.classList.add('loading');
  buttonEl.disabled = true;
  buttonEl.innerHTML = '';
  const spinner = document.createElement('span');
  spinner.className = 'btn-spinner';
  const label = document.createElement('span');
  label.textContent = text;
  buttonEl.append(spinner, label);
}

function resetButton(buttonEl) {
  buttonEl.classList.remove('loading');
  buttonEl.disabled = false;
  if (buttonEl._originalHTML) buttonEl.innerHTML = buttonEl._originalHTML;
}

// ─── CLIPBOARD ───────────────────────────────────────────────
function copyToClipboard(text, buttonEl) {
  navigator.clipboard.writeText(text).then(() => {
    if (buttonEl) {
      const orig = buttonEl.textContent;
      buttonEl.textContent = '✓';
      setTimeout(() => { buttonEl.textContent = orig; }, 1200);
    }
  }).catch(() => {
    showToast("Couldn't copy — your browser blocked clipboard access", 'error');
  });
}

// ─── CONNECTION ──────────────────────────────────────────────
async function checkAPIConnection() {
  try {
    const base = typeof getApiBaseUrl === 'function' ? getApiBaseUrl() : 'http://localhost:8000';
    const resp = await fetch(`${base}/health`, { signal: AbortSignal.timeout(3000) });
    return resp.ok;
  } catch {
    return false;
  }
}

function updateConnectionStatus(connected) {
  const badge = document.getElementById('conn-badge');
  if (!badge) return;
  badge.classList.toggle('connected', connected);
  badge.querySelector('.connection-label').textContent = connected ? 'Connected' : 'Offline';
  badge.title = connected
    ? 'Reading live data from the backend'
    : "Can't reach the backend — showing a sample dataset so the UI is still browsable";
}

// ─── BOOT ────────────────────────────────────────────────────
function hideBoot() {
  document.getElementById('boot-screen')?.classList.add('hidden');
}

// ─── FORMATTERS ──────────────────────────────────────────────
function timeAgo(isoString) {
  const then = new Date(isoString).getTime();
  if (Number.isNaN(then)) return '—';
  const seconds = Math.floor((Date.now() - then) / 1000);

  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(isoString).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function formatDayLabel(isoDate) {
  const d = new Date(isoDate + 'T00:00:00');
  if (Number.isNaN(d.getTime())) return isoDate;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// Sub-cent amounts are the norm here, so a flat "$0.00" would hide the
// numbers that matter. Scale precision to the value — and never round a
// real charge down to something that reads as free.
function formatCost(cost) {
  const n = Number(cost) || 0;
  if (n === 0) return '$0';
  if (n < 0.0001) return '<$0.0001';
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}

function formatLatency(ms) {
  const n = Number(ms) || 0;
  if (n < 1000) return `${Math.round(n)}ms`;
  return `${(n / 1000).toFixed(2)}s`;
}

function formatPercent(value, decimals = 1) {
  return `${Number(value).toFixed(decimals)}%`;
}
