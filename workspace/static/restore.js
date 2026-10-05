// Workspace restore banner (markup in templates/base.html). After a restart,
// the server holds a restore offer when sessions of the last snapshot are
// missing from tmux. Shown on every page until Restore or Dismiss, both of
// which drop the offer server-side. Plain fetch: a hiccup never disrupts the
// page, the banner just stays hidden.
(function () {
    var banner = document.getElementById('workspace-restore');
    if (!banner) return;
    var title = document.getElementById('workspace-restore-title');
    var detail = document.getElementById('workspace-restore-detail');
    var actions = banner.querySelector('.consent-banner-actions');

    function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

    function savedAt(epoch) {
        if (!epoch) return '';
        var d = new Date(epoch * 1000);
        var time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        if (d.toDateString() === new Date().toDateString()) return 'saved at ' + time;
        return 'saved ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + time;
    }

    function setButtons(disabled) {
        banner.querySelectorAll('button').forEach(function (b) { b.disabled = disabled; });
    }

    function show(d) {
        var parts = [plural(d.sessions.length, 'session')];
        if (d.agents) parts.push(plural(d.agents, 'agent'));
        var when = savedAt(d.saved_at);
        detail.textContent = parts.join(', ') + (when ? ', ' + when : '') + ': ' +
            d.sessions.map(function (s) { return s.name; }).join(', ');
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

    fetch('/api/workspace/pending', { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && d.pending) show(d); })
        .catch(function () {});

    banner.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-action]');
        if (!btn) return;
        var action = btn.getAttribute('data-action');
        setButtons(true);
        if (action === 'restore') detail.textContent = 'Restoring…';
        fetch('/api/workspace/' + action, { method: 'POST' })
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (r) {
                if (action === 'restore') done(r);
                else banner.hidden = true;
            })
            .catch(function () {
                title.textContent = "Your last workspace didn't come back";
                detail.textContent = 'That did not work. Try again.';
                setButtons(false);
            });
    });
})();
