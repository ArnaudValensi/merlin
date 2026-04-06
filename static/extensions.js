/* Extensions page — toggle, config, restart */

const Extensions = {
    _pendingChanges: new Set(),

    async toggle(extId) {
        try {
            const resp = await fetch(`/api/extensions/${extId}/toggle`, { method: 'POST' });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                alert(data.detail || 'Toggle failed');
                return;
            }
            const data = await resp.json();
            // Update toggle UI
            const toggle = document.querySelector(`[data-ext-id="${extId}"] input`);
            if (toggle) toggle.checked = data.enabled;
            // Track pending change
            this._pendingChanges.add(extId);
            this._showBanner();
        } catch (e) {
            console.error('Toggle error:', e);
        }
    },

    toggleConfig(extId) {
        const config = document.getElementById(`config-${extId}`);
        if (config) config.classList.toggle('open');
    },

    async saveConfig(extId) {
        const form = document.getElementById(`config-${extId}`);
        if (!form) return;
        const inputs = form.querySelectorAll('.ext-config-input');
        const body = {};
        inputs.forEach(input => {
            if (input.type === 'checkbox') {
                body[input.name] = input.checked ? 'true' : '';
            } else {
                body[input.name] = input.value;
            }
        });

        try {
            const resp = await fetch(`/api/extensions/${extId}/config`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                alert('Save failed');
                return;
            }
            // Show restart needed
            this._pendingChanges.add(extId + '-config');
            this._showBanner();
            // Brief flash to confirm save
            const btn = form.querySelector('.ext-config-save');
            if (btn) {
                const orig = btn.textContent;
                btn.textContent = 'Saved';
                setTimeout(() => { btn.textContent = orig; }, 1500);
            }
        } catch (e) {
            console.error('Config save error:', e);
        }
    },

    async restart() {
        try {
            await fetch('/api/restart', { method: 'POST' });
        } catch {}
        // Wait for server to come back, then reload
        this._waitAndReload();
    },

    async _waitAndReload() {
        for (let i = 0; i < 20; i++) {
            await new Promise(r => setTimeout(r, 500));
            try {
                const resp = await fetch('/api/extensions', { signal: AbortSignal.timeout(2000) });
                if (resp.ok) { window.location.reload(); return; }
            } catch {}
        }
        window.location.reload(); // give up and try anyway
    },

    onCheckboxChange(checkbox) {
        // Show/hide fields that depend on this checkbox
        const form = checkbox.closest('.ext-config');
        if (!form) return;
        const dependents = form.querySelectorAll(`[data-depends-on="${checkbox.name}"]`);
        dependents.forEach(el => {
            el.style.display = checkbox.checked ? '' : 'none';
        });
    },

    initDependencies() {
        // On page load, show/hide dependent fields based on current checkbox state
        document.querySelectorAll('.ext-config-checkbox').forEach(cb => {
            this.onCheckboxChange(cb);
        });
        this._initNotesSync();
    },

    // ---------------------------------------------------------------
    // Notes git sync: status line + test button
    // ---------------------------------------------------------------

    _initNotesSync() {
        const remoteInput = document.querySelector('input[name="NOTES_GIT_REMOTE"]');
        if (!remoteInput) return;

        const remoteField = remoteInput.closest('.ext-config-field');
        if (!remoteField) return;

        // Test connection row — insert after remote URL field
        const testRow = document.createElement('div');
        testRow.className = 'ext-config-field sync-test-row';
        testRow.setAttribute('data-depends-on', 'NOTES_GIT_SYNC');
        testRow.style.display = 'none';
        testRow.innerHTML = `
            <button class="sync-test-btn" type="button">Test Connection</button>
            <span class="sync-test-result" id="sync-test-result"></span>
        `;
        remoteField.after(testRow);

        testRow.querySelector('.sync-test-btn').addEventListener('click', () => this._testSync());

        // Sync status line — insert before the Save button
        const form = remoteInput.closest('.ext-config');
        const saveBtn = form?.querySelector('.ext-config-save');
        if (form && saveBtn) {
            const statusLine = document.createElement('div');
            statusLine.className = 'ext-config-field sync-status-line';
            statusLine.id = 'sync-status-line';
            statusLine.setAttribute('data-depends-on', 'NOTES_GIT_SYNC');
            statusLine.style.display = 'none';
            saveBtn.before(statusLine);
        }

        // Re-run dependency visibility so new elements respect checkbox state
        const checkbox = document.querySelector('input[name="NOTES_GIT_SYNC"]');
        if (checkbox) this.onCheckboxChange(checkbox);

        // Load current sync status
        this._loadSyncStatus();
    },

    async _testSync() {
        const remoteInput = document.querySelector('input[name="NOTES_GIT_REMOTE"]');
        const result = document.getElementById('sync-test-result');
        const btn = document.querySelector('.sync-test-btn');
        if (!remoteInput || !result || !btn) return;

        const url = remoteInput.value.trim();
        if (!url) {
            result.textContent = 'Enter a remote URL first';
            result.className = 'sync-test-result error';
            return;
        }

        btn.disabled = true;
        btn.textContent = 'Testing\u2026';
        result.textContent = '';
        result.className = 'sync-test-result';

        try {
            const resp = await fetch('/api/notes/sync-test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ remote_url: url }),
            });
            const data = await resp.json();
            result.textContent = data.ok ? 'Connection successful' : data.message;
            result.className = `sync-test-result ${data.ok ? 'ok' : 'error'}`;
        } catch {
            result.textContent = 'Test failed';
            result.className = 'sync-test-result error';
        } finally {
            btn.disabled = false;
            btn.textContent = 'Test Connection';
        }
    },

    async _loadSyncStatus() {
        const line = document.getElementById('sync-status-line');
        if (!line) return;

        try {
            const resp = await fetch('/api/notes/sync-status');
            if (!resp.ok) return;
            const data = await resp.json();

            if (data.last_push_ok === true) {
                line.textContent = `Last sync: ${this._timeAgo(data.last_push_at)}`;
                line.classList.add('ok');
            } else if (data.last_push_ok === false) {
                line.textContent = `Push failed: ${data.last_error || 'unknown error'}`;
                line.classList.add('error');
            } else {
                line.textContent = 'No sync yet';
                line.classList.add('muted');
            }
        } catch { /* ignore */ }
    },

    _timeAgo(isoStr) {
        if (!isoStr) return 'never';
        const seconds = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
        if (seconds < 60) return 'just now';
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes}m ago`;
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours}h ago`;
        const days = Math.floor(hours / 24);
        return `${days}d ago`;
    },

    _showBanner() {
        const banner = document.getElementById('restart-banner');
        if (banner) banner.classList.add('visible');
    }
};

document.addEventListener('DOMContentLoaded', () => Extensions.initDependencies());
