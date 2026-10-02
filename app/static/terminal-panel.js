/* Apps in the web terminal: the status-bar ▶ button for apps started from the
 * current tmux window, a toast when one starts, a docked live panel on
 * desktop and a draggable mini-player on mobile. Loaded only when the Apps
 * feature flag is on (terminal.html guards the include).
 */
(function () {
    'use strict';

    var POLL_MS = 3000;
    var TOAST_MS = 8000;
    var AGENT_INPUT_MS = 2000;

    var btn = document.getElementById('app-btn');
    var btnLabel = document.getElementById('app-btn-label');
    var btnCount = document.getElementById('app-btn-count');
    var panel = document.getElementById('app-panel');
    var divider = document.getElementById('app-divider');
    var main = document.querySelector('.main');
    var statusBar = document.getElementById('terminal-status');
    if (!btn || !panel || !main || !window.MerlinApps) return;

    var mq = window.matchMedia('(min-width: 769px)');
    function desktop() { return mq.matches; }

    var current = {session: '', windowId: '', window: ''};
    var apps = [];
    var known = null;           // "id@started_at" seen at least once (null until the first poll)
    var openId = null;
    var stream = null;
    var statsTimer = null;
    var agentUntil = 0;
    var chooser = null;
    var toastEl = null, toastTimer = null;
    var restoreSessions = false;   // the Sessions panel was docked before we took its slot

    // ---- current tmux window -------------------------------------------------

    function readCurrent() {
        var term = window.MerlinTerminal;
        if (term && term.currentWindow) {
            current.session = term.currentSession() || '';
            current.windowId = term.currentWindow() || '';
        }
    }
    document.addEventListener('merlin:terminal-window', function (event) {
        var d = event.detail || {};
        current = {session: d.session || '', windowId: d.windowId || '', window: d.window || ''};
        render();
    });

    function live(app) { return app.status === 'running' || app.status === 'starting'; }

    function windowApps() {
        if (!current.windowId) return [];
        return apps.filter(function (app) {
            return live(app) && app.origin && app.origin.tmux_window_id === current.windowId;
        }).sort(function (a, b) { return String(b.started_at).localeCompare(String(a.started_at)); });
    }

    // ---- polling + toast ------------------------------------------------------

    function key(app) { return app.id + '@' + app.started_at; }

    function poll() {
        fetch('/api/apps/sessions', {credentials: 'same-origin'})
            .then(function (r) { return r.ok ? r.json() : []; })
            .then(function (list) {
                apps = Array.isArray(list) ? list : [];
                readCurrent();
                var first = known === null;
                if (first) known = {};
                apps.forEach(function (app) {
                    var k = key(app);
                    if (known[k]) return;
                    known[k] = true;
                    if (!first && live(app) && app.origin &&
                        app.origin.tmux_window_id === current.windowId) {
                        toast(app);
                    }
                });
                render();
                if (openId && !apps.some(function (a) { return a.id === openId; })) {
                    showOverlay('The app was stopped.', null);
                }
            })
            .catch(function () {});
    }

    function toast(app) {
        if (!toastEl) {
            toastEl = document.createElement('div');
            toastEl.className = 'app-toast';
            toastEl.setAttribute('role', 'status');
            document.body.appendChild(toastEl);
        }
        var where = app.origin && app.origin.tmux_session ?
            app.origin.tmux_session + ':' + (app.origin.tmux_window_name || '') : 'this window';
        toastEl.innerHTML = '';
        var text = document.createElement('span');
        text.textContent = app.name + ' started in ' + where;
        var watch = document.createElement('button');
        watch.type = 'button';
        watch.textContent = 'Watch';
        watch.addEventListener('click', function () { hideToast(); open(app.id); });
        toastEl.appendChild(text);
        toastEl.appendChild(watch);
        toastEl.style.bottom = (bottomChrome() + 12) + 'px';
        toastEl.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(hideToast, TOAST_MS);
    }
    function hideToast() { if (toastEl) toastEl.classList.remove('show'); }

    // ---- status-bar button ----------------------------------------------------

    function render() {
        var list = windowApps();
        btn.hidden = list.length === 0;
        if (!list.length) { closeChooser(); return; }
        btnLabel.textContent = list[0].name;
        btnCount.hidden = list.length < 2;
        btnCount.textContent = list.length;
        btn.classList.toggle('active', !!openId && list.some(function (a) { return a.id === openId; }));
    }

    btn.addEventListener('click', function () {
        var list = windowApps();
        if (!list.length) return;
        if (list.length === 1) {
            if (openId === list[0].id) close(); else open(list[0].id);
            return;
        }
        if (chooser) { closeChooser(); return; }
        chooser = document.createElement('div');
        chooser.className = 'app-chooser';
        list.forEach(function (app) {
            var item = document.createElement('button');
            item.type = 'button';
            item.textContent = app.name;
            item.addEventListener('click', function () { closeChooser(); open(app.id); });
            chooser.appendChild(item);
        });
        var rect = btn.getBoundingClientRect();
        chooser.style.right = Math.max(8, window.innerWidth - rect.right) + 'px';
        chooser.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
        document.body.appendChild(chooser);
    });
    function closeChooser() { if (chooser) { chooser.remove(); chooser = null; } }
    document.addEventListener('click', function (e) {
        if (chooser && !chooser.contains(e.target) && !btn.contains(e.target)) closeChooser();
    });

    // ---- the panel ------------------------------------------------------------

    var els = {};

    function build(app) {
        panel.innerHTML = '';
        var head = document.createElement('div');
        head.className = 'app-panel-head';
        var name = document.createElement('span');
        name.className = 'app-panel-name';
        name.textContent = app ? app.name : openId;
        var chip = document.createElement('span');
        chip.className = 'app-chip';
        var pip = iconButton('Picture in picture', '<rect x="2" y="4" width="20" height="16" rx="2"/><rect x="12" y="12" width="8" height="6" rx="1"/>');
        pip.classList.add('app-pip');
        pip.hidden = !document.pictureInPictureEnabled;
        var full = iconButton('Full screen player', '<path d="M8 3H3v5M16 3h5v5M21 16v5h-5M3 16v5h5"/>');
        var x = iconButton('Close (the app keeps running)', '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>');
        head.appendChild(name);
        head.appendChild(chip);
        head.appendChild(pip);
        head.appendChild(full);
        head.appendChild(x);

        var body = document.createElement('div');
        body.className = 'app-panel-body';
        body.tabIndex = 0;
        var video = document.createElement('video');
        video.className = 'app-panel-video';
        video.muted = true;
        video.autoplay = true;
        video.setAttribute('playsinline', '');
        var overlay = document.createElement('div');
        overlay.className = 'app-panel-overlay';
        overlay.hidden = true;
        body.appendChild(video);
        body.appendChild(overlay);

        panel.appendChild(head);
        panel.appendChild(body);
        els = {head: head, chip: chip, body: body, video: video, overlay: overlay};

        x.addEventListener('click', function (e) { e.stopPropagation(); close(); });
        full.addEventListener('click', function (e) { e.stopPropagation(); location.href = '/apps/' + encodeURIComponent(openId) + '/play'; });
        pip.addEventListener('click', function (e) {
            e.stopPropagation();
            if (document.pictureInPictureElement) document.exitPictureInPicture().catch(function () {});
            else video.requestPictureInPicture().catch(function () {});
        });
        return body;
    }

    function iconButton(title, paths) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'app-icon-btn';
        b.title = title;
        b.setAttribute('aria-label', title);
        b.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + paths + '</svg>';
        return b;
    }

    function showOverlay(text, action) {
        if (!els.overlay) return;
        els.overlay.innerHTML = '';
        var p = document.createElement('p');
        p.textContent = text;
        els.overlay.appendChild(p);
        if (action) {
            var b = document.createElement('button');
            b.type = 'button';
            b.textContent = action.label;
            b.addEventListener('click', function (e) { e.stopPropagation(); action.run(); });
            els.overlay.appendChild(b);
        }
        els.overlay.hidden = false;
    }
    function hideOverlay() { if (els.overlay) els.overlay.hidden = true; }

    function onState(state, detail) {
        setTimeout(updateChip, 0);
        if (state === 'live') { hideOverlay(); return; }
        if (state === 'connecting') { showOverlay('Connecting…', null); return; }
        if (state === 'unreachable') {
            showOverlay("Can't reach " + (detail.host || 'this machine') +
                ' directly. Join the same Wi-Fi, or use Tailscale.',
                {label: 'Retry', run: function () { stream.reconnect(); }});
            return;
        }
        if (state === 'replaced') {
            showOverlay('Opened on another device.', {label: 'Watch here', run: function () { stream.reconnect(); }});
            return;
        }
        if (state === 'exited') {
            var id = openId;
            showOverlay(detail.reason === 'stopped' ? 'The app was stopped.' :
                'The app exited' + (detail.code != null ? ' (code ' + detail.code + ').' : '.'),
                {label: 'Logs', run: function () { window.open('/api/apps/sessions/' + encodeURIComponent(id) + '/logs', '_blank'); }});
            return;
        }
        if (state === 'paused') {
            showOverlay('Paused while hidden.', {label: 'Resume', run: function () { stream.reconnect(); }});
            return;
        }
        if (state === 'error') { showOverlay(detail.message || 'Stream error.', {label: 'Retry', run: function () { stream.reconnect(); }}); return; }
        if (state === 'closed') showOverlay('Disconnected.', {label: 'Retry', run: function () { stream.reconnect(); }});
    }

    function updateChip() {
        if (!stream || !els.chip) return;
        if (Date.now() < agentUntil) {
            els.chip.textContent = 'agent is pressing keys';
            els.chip.className = 'app-chip agent';
            return;
        }
        if (stream.state !== 'live') {
            els.chip.textContent = stream.state;
            els.chip.className = 'app-chip';
            return;
        }
        stream.stats().then(function (s) {
            if (!s || !els.chip || Date.now() < agentUntil) return;
            els.chip.textContent = 'LAN' + (s.rttMs != null ? ' · ' + s.rttMs + ' ms' : '') +
                (s.fps != null ? ' · ' + s.fps + ' fps' : '');
            els.chip.className = 'app-chip live';
        });
    }

    function open(id) {
        if (openId === id && stream) return;
        hideToast();  // its job is done, and on mobile it would cover the mini-player
        close(true);
        openId = id;
        var app = apps.filter(function (a) { return a.id === id; })[0];
        var body = build(app);
        panel.hidden = false;
        panel.classList.toggle('mini', !desktop());
        if (desktop()) {
            // One docked panel at a time: the app stream takes the Sessions slot.
            if (!main.classList.contains('app-open')) {
                restoreSessions = !main.classList.contains('panel-collapsed');
            }
            main.classList.add('panel-collapsed');
            main.classList.add('app-open');
            if (divider) divider.hidden = false;
        } else {
            placeMini();
        }
        stream = MerlinApps.connect({
            id: id,
            video: els.video,
            onState: onState,
            onAgentInput: function () { agentUntil = Date.now() + AGENT_INPUT_MS; updateChip(); }
        });
        if (desktop()) MerlinApps.bindDesktopInput(body, els.video, function () { return stream; });
        else bindMini();
        clearInterval(statsTimer);
        statsTimer = setInterval(updateChip, 1000);
        render();
    }

    function close(keepLayout) {
        if (stream) { stream.destroy(); stream = null; }
        clearInterval(statsTimer);
        if (document.pictureInPictureElement === els.video) document.exitPictureInPicture().catch(function () {});
        openId = null;
        els = {};
        panel.innerHTML = '';
        panel.hidden = true;
        panel.classList.remove('mini');
        panel.removeAttribute('style');     // mini-player positions
        panel.removeAttribute('data-corner');
        if (divider) divider.hidden = true;
        if (!keepLayout && main.classList.contains('app-open')) {
            main.classList.remove('app-open');
            if (restoreSessions && desktop()) main.classList.remove('panel-collapsed');
            restoreSessions = false;
        }
        render();
    }

    // Opening the Sessions panel (desktop) closes the app panel.
    new MutationObserver(function () {
        if (openId && desktop() && !main.classList.contains('panel-collapsed')) {
            restoreSessions = false;
            close();
        }
    }).observe(main, {attributes: true, attributeFilter: ['class']});

    // Crossing the breakpoint (rotation, resize) rebuilds the panel: docked
    // with keyboard and mouse on desktop, a view-only mini-player on mobile.
    mq.addEventListener('change', function () {
        if (!openId) return;
        var id = openId, keep = restoreSessions;
        close(true);
        open(id);
        restoreSessions = keep;
    });

    // ---- desktop divider -----------------------------------------------------

    var storedW = parseInt(localStorage.getItem('app-panel-w'), 10);
    if (storedW) main.style.setProperty('--app-w', storedW + 'px');
    if (divider) {
        var dragging = false, lastW = 0;
        divider.addEventListener('pointerdown', function (e) {
            if (!desktop()) return;
            dragging = true; lastW = 0;
            try { divider.setPointerCapture(e.pointerId); } catch (x) {}
            e.preventDefault();
        });
        divider.addEventListener('pointermove', function (e) {
            if (!dragging) return;
            var w = window.innerWidth - e.clientX;
            lastW = Math.max(240, Math.min(w, Math.round(window.innerWidth * 0.75)));
            main.style.setProperty('--app-w', lastW + 'px');
        });
        function endDrag(e) {
            if (!dragging) return;
            dragging = false;
            try { divider.releasePointerCapture(e.pointerId); } catch (x) {}
            if (lastW) localStorage.setItem('app-panel-w', lastW);
        }
        divider.addEventListener('pointerup', endDrag);
        divider.addEventListener('pointercancel', endDrag);
    }

    // ---- mobile mini-player ----------------------------------------------------

    var CORNERS = ['br', 'bl', 'tr', 'tl'];

    function corner() {
        var c = localStorage.getItem('app-mini-corner');
        return CORNERS.indexOf(c) >= 0 ? c : 'br';
    }

    /** Height of the terminal chrome at the bottom (key toolbar + status bar). */
    function bottomChrome() {
        var top = window.innerHeight;
        [document.getElementById('terminal-toolbar'), statusBar].forEach(function (el) {
            if (!el || !el.offsetHeight) return;
            var r = el.getBoundingClientRect();
            if (r.height && r.top < top && r.bottom > window.innerHeight / 2) top = r.top;
        });
        return window.innerHeight - top;
    }

    function placeMini(c) {
        c = c || corner();
        var margin = 12;
        var bottom = bottomChrome() + margin;
        panel.style.left = panel.style.right = panel.style.top = panel.style.bottom = '';
        panel.style.transform = '';
        if (c[0] === 'b') panel.style.bottom = bottom + 'px'; else panel.style.top = margin + 'px';
        if (c[1] === 'r') panel.style.right = margin + 'px'; else panel.style.left = margin + 'px';
        panel.setAttribute('data-corner', c);
    }

    function bindMini() {
        var start = null, moved = false;
        els.body.addEventListener('pointerdown', function (e) {
            start = {x: e.clientX, y: e.clientY, rect: panel.getBoundingClientRect()};
            moved = false;
            try { els.body.setPointerCapture(e.pointerId); } catch (x) {}
        });
        els.body.addEventListener('pointermove', function (e) {
            if (!start) return;
            var dx = e.clientX - start.x, dy = e.clientY - start.y;
            if (!moved && Math.abs(dx) + Math.abs(dy) < 8) return;
            moved = true;
            panel.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
        });
        els.body.addEventListener('pointerup', function (e) {
            if (!start) return;
            var wasMoved = moved;
            var r = panel.getBoundingClientRect();
            start = null;
            if (!wasMoved) {
                if (els.overlay && !els.overlay.hidden && els.overlay.contains(e.target)) return;
                location.href = '/apps/' + encodeURIComponent(openId) + '/play';
                return;
            }
            var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
            var c = (cy > window.innerHeight / 2 ? 'b' : 't') + (cx > window.innerWidth / 2 ? 'r' : 'l');
            localStorage.setItem('app-mini-corner', c);
            placeMini(c);
        });
        els.body.addEventListener('pointercancel', function () { start = null; placeMini(); });
    }

    window.addEventListener('resize', function () { if (openId && !desktop()) placeMini(); });
    var toolbar = document.getElementById('terminal-toolbar');
    if (toolbar && window.ResizeObserver) {
        new ResizeObserver(function () { if (openId && !desktop()) placeMini(); }).observe(toolbar);
    }

    // ---- start ------------------------------------------------------------------

    readCurrent();
    poll();
    setInterval(poll, POLL_MS);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
    window.MerlinAppPanel = {open: open, close: close, get openId() { return openId; }};
})();
