/* Settings page */

const Settings = {
    toggleReveal(inputId) {
        const input = document.getElementById(inputId);
        if (!input) return;
        input.type = input.type === 'password' ? 'text' : 'password';
    },

    async _save(body, toastId) {
        try {
            const resp = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                alert('Save failed' + (data.detail ? ': ' + data.detail : ''));
                return;
            }
            const data = await resp.json();
            // Show toast
            this._toast(toastId, 'Saved');
            // Show restart banner if needed
            if (data.restart_required) {
                const banner = document.getElementById('restart-banner');
                if (banner) banner.classList.add('visible');
            }
        } catch (e) {
            console.error('Settings save error:', e);
        }
    },

    savePassword() {
        const val = document.getElementById('password-input').value;
        this._save({ DASHBOARD_PASS: val }, 'toast-password');
    },

    saveOpenAIKey() {
        const val = document.getElementById('openai-input').value;
        this._save({ OPENAI_API_KEY: val }, 'toast-openai');
    },

    // ── Public URL ──
    async savePublicUrl() {
        const val = document.getElementById('public-url-input').value.trim();
        await this._save({ MERLIN_DASHBOARD_URL: val }, 'toast-public-url');
        this.loadPublicUrl();
    },

    // In SaaS mode the field sits behind an override checkbox: unchecking
    // clears the setting immediately (back to the Merlin Cloud address).
    async onPublicUrlToggle() {
        const on = document.getElementById('public-url-override').checked;
        const input = document.getElementById('public-url-input');
        input.disabled = !on;
        if (on) {
            input.focus();
        } else if (input.value.trim()) {
            input.value = '';
            await this._save({ MERLIN_DASHBOARD_URL: '' }, 'toast-public-url');
            this.loadPublicUrl();
        }
    },

    async loadPublicUrl() {
        const input = document.getElementById('public-url-input');
        if (!input) return;
        const data = await API.get('/api/settings');
        if (!data) return;
        input.value = data.public_url || '';
        const checkbox = document.getElementById('public-url-override');
        if (checkbox) {
            checkbox.checked = !!data.public_url;
            input.disabled = !data.public_url;
        }
        const eff = document.getElementById('public-url-effective');
        if (eff) {
            const labels = {
                override: 'custom override',
                saas: 'via Merlin Cloud',
                slug: 'via Merlin Cloud',
                ip: 'detected local address',
            };
            const label = labels[data.public_url_source] || data.public_url_source;
            eff.textContent = 'Effective: ' + data.effective_public_url + ' (' + label + ')';
        }
    },


    async restart() {
        try {
            await fetch('/api/restart', { method: 'POST' });
        } catch {}
        for (let i = 0; i < 20; i++) {
            await new Promise(r => setTimeout(r, 500));
            try {
                const resp = await fetch('/api/settings', { signal: AbortSignal.timeout(2000) });
                if (resp.ok) { window.location.reload(); return; }
            } catch {}
        }
        window.location.reload();
    },

    async updateAndRestart() {
        const btn = document.getElementById('update-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'Updating...'; }
        try {
            const resp = await fetch('/api/update', { method: 'POST' });
            const data = await resp.json();
            if (!data.ok) {
                alert(data.error || 'Update failed');
                if (btn) { btn.disabled = false; btn.textContent = 'Update & Restart'; }
                return;
            }
            if (btn) btn.textContent = 'Restarting...';
        } catch {
            if (btn) { btn.disabled = false; btn.textContent = 'Update & Restart'; }
            return;
        }
        // Poll until server comes back
        for (let i = 0; i < 30; i++) {
            await new Promise(r => setTimeout(r, 500));
            try {
                const resp = await fetch('/api/settings', { signal: AbortSignal.timeout(2000) });
                if (resp.ok) { window.location.reload(); return; }
            } catch {}
        }
        window.location.reload();
    },

    _toast(id, msg) {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = msg;
        el.classList.add('visible');
        setTimeout(() => { el.classList.remove('visible'); }, 2000);
    }
};

// Populate update section on settings page
(async function initUpdateSection() {
    const section = document.getElementById('update-section');
    if (!section) return;

    const data = await API.get('/api/version');
    if (!data) return;

    const currentEl = document.getElementById('update-current');
    const latestEl = document.getElementById('update-latest');
    const actionsEl = document.getElementById('update-actions');

    if (currentEl) currentEl.textContent = 'v' + data.current;

    if (data.update_available && data.latest) {
        if (latestEl) latestEl.textContent = '\u2192 v' + data.latest + ' available';

        const changelog = document.createElement('a');
        // CHANGELOG.md at the target tag: the release flow commits the
        // changelog before tagging, so this always exists and the new
        // version's section is at the top. No GitHub Release objects.
        changelog.href = 'https://github.com/ArnaudValensi/merlin/blob/v' + data.latest + '/CHANGELOG.md';
        changelog.target = '_blank';
        changelog.rel = 'noopener';
        changelog.textContent = 'Changelog';

        if (data.dev_mode) {
            const hint = document.createElement('span');
            hint.className = 'update-hint';
            hint.textContent = 'git pull to update';
            actionsEl.append(changelog, hint);
        } else {
            const btn = document.createElement('button');
            btn.id = 'update-btn';
            btn.className = 'update-btn';
            btn.textContent = 'Update & Restart';
            btn.addEventListener('click', () => Settings.updateAndRestart());
            actionsEl.append(changelog, btn);
        }
    } else {
        if (latestEl) latestEl.textContent = '(up to date)';
    }
})();

document.addEventListener('DOMContentLoaded', () => {
    Settings.loadPublicUrl();
});
