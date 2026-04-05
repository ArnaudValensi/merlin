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
                alert('Save failed');
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

    _toast(id, msg) {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = msg;
        el.classList.add('visible');
        setTimeout(() => { el.classList.remove('visible'); }, 2000);
    }
};
