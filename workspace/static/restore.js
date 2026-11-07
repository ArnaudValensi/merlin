// Workspace restore banner (markup in templates/base.html). The server holds
// a restore offer when sessions of the last snapshot are missing from tmux:
// after a restart, or when tmux died while Merlin kept running. Shown on
// every page until Restore or Dismiss, both of which drop the offer
// server-side. Fetched on load, then every 15 s while the banner is hidden
// and the page visible, and when the page becomes visible again, so a crash
// shows up without a reload. Plain fetch: a hiccup never disrupts the page,
// the banner just stays hidden. Every field beyond the first release's is
// read as optional: a server that has not restarted still answers the old
// shape.
(function () {
    var banner = document.getElementById('workspace-restore');
    if (!banner) return;
    var title = document.getElementById('workspace-restore-title');
    var detail = document.getElementById('workspace-restore-detail');
    var actions = banner.querySelector('.consent-banner-actions');
    var POLL_MS = 15000;
    var STARTUP_TITLE = "Your last workspace didn't come back";
    var offerId = null;

    function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

    function clock(epoch) {
        return new Date(epoch * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function savedAt(epoch) {
        if (!epoch) return '';
        var d = new Date(epoch * 1000);
        if (d.toDateString() === new Date().toDateString()) return 'saved at ' + clock(epoch);
        return 'saved ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + clock(epoch);
    }

    function setButtons(disabled) {
        banner.querySelectorAll('button').forEach(function (b) { b.disabled = disabled; });
    }

    function show(d) {
        offerId = d.id === undefined ? null : d.id;
        var crashed = d.reason === 'server-changed' || d.reason === 'server-gone';
        title.textContent = crashed && d.crashed_at
            ? 'tmux stopped at ' + clock(d.crashed_at)
            : (crashed ? 'tmux stopped' : STARTUP_TITLE);
        var parts = [plural(d.sessions.length, 'session')];
        if (d.agents) parts.push(plural(d.agents, 'agent'));
        var when = savedAt(d.saved_at);
        detail.textContent = parts.join(', ') + (when ? ', ' + when : '') + ': ' +
            d.sessions.map(function (s) { return s.name; }).join(', ');
        actions.hidden = false;
        setButtons(false);
        banner.hidden = false;
    }

    function done(r) {
        var msg = 'Restored ' + plural(r.restored.length, 'session') +
            (r.agents ? ', ' + plural(r.agents, 'agent') + ' resumed' : '') + '.';
        if (r.skipped.length) msg += ' Skipped (name in use): ' + r.skipped.join(', ') + '.';
        if (r.failed.length) msg += ' Failed: ' + r.failed.join(', ') + '.';
        title.textContent = 'Workspace restored';
        detail.textContent = msg;
        actions.hidden = true;
        setTimeout(function () { banner.hidden = true; }, r.skipped.length || r.failed.length ? 10000 : 5000);
    }

    function poll() {
        if (!banner.hidden || document.visibilityState !== 'visible') return;
        fetch('/api/workspace/pending', { headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) { if (d && d.pending && banner.hidden) show(d); })
            .catch(function () {});
    }

    poll();
    setInterval(poll, POLL_MS);
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') poll();
    });

    banner.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-action]');
        if (!btn) return;
        var action = btn.getAttribute('data-action');
        setButtons(true);
        if (action === 'restore') detail.textContent = 'Restoring…';
        var init = { method: 'POST' };
        if (action === 'dismiss' && offerId !== null) {
            // Name the offer this page shows: a newer one, published while
            // this one was on screen, must stay. An older server ignores it.
            init.headers = { 'Content-Type': 'application/json' };
            init.body = JSON.stringify({ id: offerId });
        }
        fetch('/api/workspace/' + action, init)
            .then(function (r) {
                if (r.ok) return r.json();
                // The server says why (a restore already running, tmux
                // unreadable): show that rather than a generic line.
                return r.json().catch(function () { return {}; }).then(function (body) {
                    throw new Error(body && body.detail ? body.detail : String(r.status));
                });
            })
            .then(function (r) {
                if (action === 'restore') done(r);
                else banner.hidden = true;
            })
            .catch(function (err) {
                title.textContent = STARTUP_TITLE;
                detail.textContent = (err && err.message && isNaN(err.message))
                    ? 'That did not work: ' + err.message + '. Try again.'
                    : 'That did not work. Try again.';
                setButtons(false);
            });
    });
})();
