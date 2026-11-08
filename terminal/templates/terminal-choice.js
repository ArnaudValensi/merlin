/* Which terminal this browser uses: the classic one (the default) or the
 * native one (/terminal/native). The choice is per browser, kept in
 * localStorage, and set from the terminal menu or with ?terminal=native (or
 * classic) on either page. /terminal leads to the chosen page, keeping the
 * rest of its URL (a deep link's ?target=). A native page that cannot run
 * here (a tmux older than it needs) sends the tab back to the classic one,
 * which says why.
 *
 * Inlined at the top of both pages, before anything opens a socket: a page
 * that is leaving sets `leaving`, and the classic page's scripts then do
 * nothing. */
var MerlinTerminalChoice = (function () {
    'use strict';

    const KEY = 'merlin-terminal';
    const FALLBACK_KEY = 'merlin-terminal-fallback';
    const PATHS = { classic: '/terminal', native: '/terminal/native' };

    function read(store, key) {
        try { return store ? store.getItem(key) : null; } catch (e) { return null; }
    }
    function write(store, key, value) {
        try {
            if (!store) return;
            if (value == null) store.removeItem(key);
            else store.setItem(key, value);
        } catch (e) { /* storage unavailable: the default stays */ }
    }
    // The storages themselves can throw (blocked site data).
    function local() { try { return window.localStorage; } catch (e) { return null; } }
    function session() { try { return window.sessionStorage; } catch (e) { return null; } }
    function kind(value) {
        return value === 'native' || value === 'classic' ? value : null;
    }

    // What a page should do, from its kind, its URL query, the stored choice
    // and the fallback note: { store, clearFallback, go, query }. `go` is the
    // path to leave for (null: stay), `query` the query to keep (the
    // ?terminal= parameter dropped), `store` the choice to remember (null:
    // unchanged). Pure, for the tests.
    function plan(page, search, stored, fallback) {
        const params = new URLSearchParams(search || '');
        const asked = kind(params.get('terminal'));
        const hadParam = params.has('terminal');
        // The rest of the query exactly as it was (a deep link's target).
        const rest = String(search || '').replace(/^\?/, '').split('&').filter((part) => {
            if (!part) return false;
            const name = part.split('=')[0].replace(/\+/g, ' ');
            try { return decodeURIComponent(name) !== 'terminal'; } catch (e) { return true; }
        });
        const query = rest.length ? '?' + rest.join('&') : '';
        const chosen = asked || kind(stored) || 'classic';
        const out = { store: asked, clearFallback: asked === 'native', go: null, query, dropParam: hadParam };
        if (page === 'classic') {
            const stayed = !!fallback && asked !== 'native';
            if (chosen === 'native' && !stayed) out.go = PATHS.native;
        } else if (page === 'native') {
            if (asked === 'classic') out.go = PATHS.classic;
        }
        return out;
    }

    const Choice = {
        KEY,
        FALLBACK_KEY,
        PATHS,
        plan,
        leaving: false,
        page: null,
        get() { return kind(read(local(), KEY)) || 'classic'; },
        set(value) { write(local(), KEY, kind(value) || 'classic'); },
        fallback() { return read(session(), FALLBACK_KEY) || ''; },

        // Run first on each page. True when the page is leaving.
        route(page) {
            this.page = page;
            const p = plan(page, location.search, read(local(), KEY), this.fallback());
            if (p.store) this.set(p.store);
            if (p.clearFallback) write(session(), FALLBACK_KEY, null);
            if (p.go) {
                this.leaving = true;
                document.documentElement.style.visibility = 'hidden';
                location.replace(p.go + p.query + location.hash);
                return true;
            }
            if (p.dropParam) {
                try { history.replaceState(null, '', location.pathname + p.query + location.hash); } catch (e) { /* keep it */ }
            }
            return false;
        },

        // The native page cannot run here: back to the classic page for this
        // tab, which shows `why`.
        fallBack(why) {
            write(session(), FALLBACK_KEY, why || 'The new terminal is unavailable here.');
            this.leaving = true;
            location.replace(PATHS.classic + location.search + location.hash);
        },

        // Remember `value` and open its page, keeping the URL's query.
        switchTo(value) {
            value = kind(value) || 'classic';
            this.set(value);
            if (value === 'native') write(session(), FALLBACK_KEY, null);
            if (value === this.page) return;
            this.leaving = true;
            const p = plan(null, location.search, null, '');
            location.assign(PATHS[value] + p.query + location.hash);
        },

        // The terminal menu (terminal-panels.html): the two terminals, and
        // whatever the page adds below them.
        bindMenu() {
            const btn = document.getElementById('term-menu-btn');
            const menu = document.getElementById('term-menu');
            if (!btn || !menu) return;
            const items = menu.querySelectorAll('[data-terminal]');
            const note = document.getElementById('term-menu-note');
            const render = () => {
                items.forEach((item) => item.setAttribute('aria-checked', String(item.dataset.terminal === this.page)));
                if (note) {
                    const why = this.fallback();
                    note.textContent = why;
                    note.hidden = !why;
                }
            };
            const close = () => { menu.hidden = true; btn.classList.remove('active'); };
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (!menu.hidden) { close(); return; }
                render();
                // Above its button, its right edge on the button's.
                const r = btn.getBoundingClientRect();
                menu.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
                menu.style.bottom = Math.max(8, window.innerHeight - r.top + 6) + 'px';
                menu.hidden = false;
                btn.classList.add('active');
            });
            // No item takes the focus from the terminal's input.
            menu.addEventListener('mousedown', (e) => e.preventDefault());
            items.forEach((item) => item.addEventListener('click', () => {
                close();
                this.switchTo(item.dataset.terminal);
            }));
            document.addEventListener('click', (e) => {
                if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) close();
            });
            document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !menu.hidden) close(); });
            this.closeMenu = close;
        },

        // On the classic page of a tab the native page sent back: one line
        // saying why, over the terminal.
        showFallback() {
            const why = this.fallback();
            const host = document.querySelector('.main-banners');
            if (!why || !host || this.get() !== 'native') return;
            const bar = document.createElement('div');
            bar.className = 'term-fallback';
            bar.setAttribute('role', 'status');
            const text = document.createElement('span');
            text.textContent = why;
            const close = document.createElement('button');
            close.type = 'button';
            close.textContent = '×';
            close.title = 'Dismiss';
            close.setAttribute('aria-label', 'Dismiss');
            close.addEventListener('click', () => bar.remove());
            bar.append(text, close);
            host.appendChild(bar);
        },
    };
    return Choice;
})();

if (typeof module !== 'undefined' && module.exports) {
    module.exports = MerlinTerminalChoice;
}
