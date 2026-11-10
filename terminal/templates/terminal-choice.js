/* Which terminal this browser uses: the native one (/terminal/native, the
 * default) or the legacy one (/terminal), deprecated and kept as a fallback
 * until it is removed. The choice is per browser, kept in localStorage, and
 * set from Merlin's settings page (static/settings.js, the same key) or with
 * ?terminal=classic (or native) on either page. /terminal leads to the chosen
 * page, keeping the rest of its URL (a deep link's ?target=). A native page
 * that cannot run here (a tmux older than it needs) sends the tab back to the
 * legacy one, which says why.
 *
 * Inlined at the top of both pages, before anything opens a socket: a page
 * that is leaving sets `leaving`, and the legacy page's scripts then do
 * nothing. */
var MerlinTerminalChoice = (function () {
    'use strict';

    const KEY = 'merlin-terminal';
    const FALLBACK_KEY = 'merlin-terminal-fallback';
    // Set by the fall back itself, consumed by the classic page it lands on:
    // that page says why even when the saved choice is not the native one (a
    // direct visit to /terminal/native).
    const FELL_BACK_KEY = 'merlin-terminal-fell-back';
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
        const chosen = asked || kind(stored) || 'native';
        const out = { store: asked, clearFallback: asked === 'native', go: null, query, dropParam: hadParam };
        if (page === 'classic') {
            const stayed = !!fallback && asked !== 'native';
            if (chosen === 'native' && !stayed) out.go = PATHS.native;
        } else if (page === 'native') {
            if (asked === 'classic') out.go = PATHS.classic;
        }
        return out;
    }

    // Whether a page shows the one-line notice: only the legacy page, when
    // this tab fell back (`fallback`), right after the fall back (`fellBack`)
    // or while the choice is the native terminal (each /terminal load then
    // lands here instead). Pure, for the tests.
    function showsFallback(page, stored, fallback, fellBack) {
        return page === 'classic' && !!fallback && (!!fellBack || (kind(stored) || 'native') === 'native');
    }

    const Choice = {
        KEY,
        FALLBACK_KEY,
        PATHS,
        plan,
        showsFallback,
        leaving: false,
        page: null,
        get() { return kind(read(local(), KEY)) || 'native'; },
        set(value) { write(local(), KEY, kind(value) || 'native'); },
        fallback() { return read(session(), FALLBACK_KEY) || ''; },

        // Run first on each page. True when the page is leaving.
        route(page) {
            this.page = page;
            const p = plan(page, location.search, read(local(), KEY), this.fallback());
            if (p.store) this.set(p.store);
            if (p.clearFallback) this.clearFallback();
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

        // The native page cannot run here: back to the legacy page for this
        // tab, which shows `why`.
        fallBack(why) {
            write(session(), FALLBACK_KEY, why || 'The new terminal is unavailable here.');
            write(session(), FELL_BACK_KEY, '1');
            this.leaving = true;
            location.replace(PATHS.classic + location.search + location.hash);
        },

        // Remember `value` and open its page, keeping the URL's query.
        switchTo(value) {
            value = kind(value) || 'native';
            this.set(value);
            if (value === 'native') this.clearFallback();
            if (value === this.page) return;
            this.leaving = true;
            const p = plan(null, location.search, null, '');
            location.assign(PATHS[value] + p.query + location.hash);
        },

        // The native terminal works here (it attached): an old fall back of
        // this tab no longer holds.
        clearFallback() {
            write(session(), FALLBACK_KEY, null);
            write(session(), FELL_BACK_KEY, null);
        },

        // On the legacy page of a tab the native page sent back: one line
        // saying why, over the terminal.
        showFallback() {
            const why = this.fallback();
            const fellBack = read(session(), FELL_BACK_KEY);
            if (this.page === 'classic') write(session(), FELL_BACK_KEY, null);
            const host = document.querySelector('.main-banners');
            if (!host || !showsFallback(this.page, read(local(), KEY), why, fellBack)) return;
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
