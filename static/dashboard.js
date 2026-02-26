/* Merlin Dashboard — Shared JS */

const API = {
    async get(url) {
        const resp = await fetch(url);
        if (resp.status === 401) {
            window.location.reload();
            return null;
        }
        return resp.json();
    }
};

// Smart auto-refresh: poll /api/last-modified, only refresh when data changes
const Refresh = {
    _lastMtime: null,
    _interval: null,
    _callbacks: [],

    register(callback) {
        this._callbacks.push(callback);
    },

    start(intervalMs = 5000) {
        this._interval = setInterval(() => this._check(), intervalMs);
    },

    async _check() {
        const data = await API.get('/api/last-modified');
        if (!data) return;
        if (this._lastMtime !== null && data.mtime !== this._lastMtime) {
            for (const cb of this._callbacks) {
                try { await cb(); } catch (e) { console.error('Refresh callback error:', e); }
            }
        }
        this._lastMtime = data.mtime;
    }
};

// Time formatting
function formatTime(isoString) {
    const d = new Date(isoString);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatDateTime(isoString) {
    const d = new Date(isoString);
    return d.toLocaleString([], {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit'
    });
}

function formatDateTimeFull(isoString) {
    const d = new Date(isoString);
    return d.toLocaleString();
}

function utcString(isoString) {
    return new Date(isoString).toISOString();
}

function timeAgo(isoString) {
    const seconds = (Date.now() - new Date(isoString).getTime()) / 1000;
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    return Math.floor(seconds / 86400) + 'd ago';
}

function formatDuration(seconds) {
    if (seconds < 1) return seconds.toFixed(2) + 's';
    if (seconds < 60) return seconds.toFixed(1) + 's';
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return m + 'm ' + s + 's';
}

function formatCost(usd) {
    if (usd == null) return '—';
    if (usd < 0.01) return '<$0.01';
    return '$' + usd.toFixed(2);
}

// Badge HTML
function typeBadge(type) {
    const labels = {
        invocation: 'Invocation',
        bot_event: 'Bot',
        cron_dispatch: 'Cron'
    };
    return `<span class="feed-badge badge-${type}">${labels[type] || type}</span>`;
}

function statusBadge(event) {
    const isError = event.exit_code != null && event.exit_code !== 0 || event.event === 'error';
    if (isError) return `<span class="feed-badge badge-error">Error</span>`;
    return `<span class="feed-badge badge-success">OK</span>`;
}

// Summary line for feed
function eventSummary(event) {
    switch (event.type) {
        case 'invocation': {
            const parts = [`${event.caller || 'unknown'} — ${formatDuration(event.duration || 0)}`];
            if (event.cost_usd != null) parts.push(formatCost(event.cost_usd));
            parts.push(`exit ${event.exit_code ?? '?'}`);
            return parts.join(' — ');
        }
        case 'bot_event':
            if (event.event === 'transcription') {
                const text = (event.content || '').substring(0, 80);
                return `🎤 ${text}${event.content && event.content.length > 80 ? '...' : ''} (${formatDuration(event.duration || 0)})`;
            }
            return event.details || event.event || '';
        case 'cron_dispatch':
            return `${event.job_id || '?'} — ${event.event || ''} ${event.duration ? '— ' + formatDuration(event.duration) : ''}`;
        default:
            return JSON.stringify(event).slice(0, 80);
    }
}

// Sidebar active state
document.addEventListener('DOMContentLoaded', () => {
    const path = window.location.pathname;
    document.querySelectorAll('.sidebar-nav a').forEach(a => {
        const href = a.getAttribute('href');
        if (href === path || (href !== '/' && path.startsWith(href))) a.classList.add('active');
    });

    // Hamburger toggle
    const hamburger = document.querySelector('.hamburger');
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.querySelector('.sidebar-overlay');
    if (hamburger) {
        hamburger.addEventListener('click', () => {
            sidebar.classList.toggle('open');
            overlay.classList.toggle('open');
        });
        if (overlay) {
            overlay.addEventListener('click', () => {
                sidebar.classList.remove('open');
                overlay.classList.remove('open');
            });
        }
    }

    // Bot status dot
    updateBotStatus();
});

async function updateBotStatus() {
    const dot = document.querySelector('.bot-status');
    if (!dot) return;
    try {
        const health = await API.get('/api/health');
        if (health && health.bot_start_time) {
            dot.classList.add('online');
            dot.classList.remove('offline');
            dot.title = 'Online since ' + formatDateTimeFull(health.bot_start_time);
        } else {
            dot.classList.add('offline');
            dot.classList.remove('online');
        }
    } catch {
        dot.classList.add('offline');
    }
}

// Chart.js defaults for dark theme
function configureChartDefaults() {
    if (typeof Chart === 'undefined') return;
    Chart.defaults.color = '#8b8fa3';
    Chart.defaults.borderColor = '#2e3347';
    Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif";
}

// Navigation helper
function navigateTo(page, params = {}) {
    const url = new URL(page, window.location.origin);
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
    window.location.href = url.toString();
}
