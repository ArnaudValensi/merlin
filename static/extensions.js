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
    },

    _showBanner() {
        const banner = document.getElementById('restart-banner');
        if (banner) banner.classList.add('visible');
    }
};

document.addEventListener('DOMContentLoaded', () => Extensions.initDependencies());
