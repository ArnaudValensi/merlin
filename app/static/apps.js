/* Apps page: saved apps (launchers) and running apps, as cards. */
(function () {
    'use strict';

    var POLL_MS = 3000;

    var root = document.getElementById('apps-app');
    var grid = document.getElementById('apps-grid');
    var empty = document.getElementById('apps-empty');
    var formModal = document.getElementById('apps-form-modal');
    var form = document.getElementById('apps-form');
    var formError = document.getElementById('apps-form-error');
    var logsModal = document.getElementById('apps-logs-modal');
    var logsText = document.getElementById('apps-logs-text');
    var homeDir = root.getAttribute('data-home') || '/';
    var touch = window.matchMedia('(pointer: coarse)').matches;

    var saved = [];
    var running = [];
    var editing = null;      // saved id being edited, or null for a new app
    var logsFor = null;
    var errors = {};         // card id -> last launch error

    function api(path, options) {
        options = options || {};
        options.credentials = 'same-origin';
        if (options.body && typeof options.body !== 'string') {
            options.headers = {'Content-Type': 'application/json'};
            options.body = JSON.stringify(options.body);
        }
        return fetch('/api/apps' + path, options).then(function (r) {
            return r.text().then(function (text) {
                var data = null;
                try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
                if (!r.ok) throw new Error((data && data.detail) || r.statusText);
                return data;
            });
        });
    }

    // ---- sizes ---------------------------------------------------------------

    /** This device's screen, landscape, in pixels, at most 1920 wide, multiples of 8. */
    function fitSize() {
        var dpr = window.devicePixelRatio || 1;
        var w = Math.max(screen.width, screen.height) * dpr;
        var h = Math.min(screen.width, screen.height) * dpr;
        if (w > 1920) { h = h * 1920 / w; w = 1920; }
        w = Math.max(320, Math.round(w / 8) * 8);
        h = Math.max(240, Math.round(h / 8) * 8);
        return w + 'x' + h;
    }

    function shellQuote(arg) {
        return /^[A-Za-z0-9_\-.\/=:,+@%]+$/.test(arg) ? arg : "'" + arg.replace(/'/g, "'\\''") + "'";
    }

    function keysText(keys) {
        return Object.keys(keys || {}).map(function (k) { return k + '=' + keys[k]; }).join(',');
    }

    // ---- rendering --------------------------------------------------------------

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        return node;
    }

    function button(label, onClick, cls) {
        var b = el('button', cls || '', label);
        b.type = 'button';
        b.addEventListener('click', onClick);
        return b;
    }

    /** A button that asks for a second tap before acting. */
    function confirmButton(label, confirmLabel, onConfirm) {
        var armed = null;
        var b = button(label, function () {
            if (!armed) {
                b.classList.add('confirm');
                b.textContent = confirmLabel;
                armed = setTimeout(function () { armed = null; b.classList.remove('confirm'); b.textContent = label; }, 3000);
                return;
            }
            clearTimeout(armed);
            b.disabled = true;
            onConfirm();
        }, 'danger');
        return b;
    }

    function loadThumb(box, id, version) {
        var img = new Image();
        img.onload = function () {
            box.style.backgroundImage = 'url("' + img.src + '")';
            box.classList.remove('missing');
            var placeholder = box.querySelector('.apps-thumb-placeholder');
            if (placeholder) placeholder.remove();
        };
        img.src = '/api/apps/sessions/' + encodeURIComponent(id) + '/thumb?v=' + version;
    }

    function thumb(id) {
        var box = el('div', 'apps-thumb missing');
        box.setAttribute('data-thumb', id);
        var placeholder = el('div', 'apps-thumb-placeholder');
        placeholder.innerHTML = '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="M10 9l5 2-5 2z"/></svg>';
        box.appendChild(placeholder);
        loadThumb(box, id, Math.floor(Date.now() / 30000));
        return box;
    }

    /** Thumbnails appear when a viewer leaves: retry the missing ones. */
    function retryThumbs() {
        grid.querySelectorAll('.apps-thumb.missing').forEach(function (box) {
            loadThumb(box, box.getAttribute('data-thumb'), Date.now());
        });
    }

    function card(opts) {
        var c = el('article', 'apps-card');
        c.setAttribute('data-id', opts.id);
        c.appendChild(thumb(opts.id));
        var body = el('div', 'apps-card-body');
        var top = el('div', 'apps-card-top');
        top.appendChild(el('h2', 'apps-card-name', opts.name));
        var status = opts.session ? opts.session.status : 'stopped';
        var label = {running: 'Running', starting: 'Starting', exited: 'Exited', stopped: 'Stopped'}[status] || status;
        if (status === 'exited' && opts.session.exit_code != null) label += ' (' + opts.session.exit_code + ')';
        top.appendChild(el('span', 'apps-status ' + status, label));
        body.appendChild(top);
        body.appendChild(el('p', 'apps-card-cmd', opts.command));
        if (opts.session && opts.session.origin && opts.session.origin.kind === 'terminal') {
            body.appendChild(el('p', 'apps-card-origin', 'started in ' + opts.session.origin.tmux_session +
                ':' + (opts.session.origin.tmux_window_name || '')));
        }
        if (errors[opts.id]) body.appendChild(el('p', 'apps-form-error', errors[opts.id]));
        var actions = el('div', 'apps-card-actions');
        opts.actions.forEach(function (a) { actions.appendChild(a); });
        body.appendChild(actions);
        c.appendChild(body);
        return c;
    }

    function render() {
        grid.innerHTML = '';
        var byId = {};
        running.forEach(function (s) { byId[s.id] = s; });

        saved.forEach(function (app) {
            var session = byId[app.id];
            delete byId[app.id];
            var live = session && (session.status === 'running' || session.status === 'starting');
            var actions = [];
            if (live) {
                actions.push(button('Resume', function () { location.href = session.url; }, 'primary'));
                actions.push(confirmButton('Stop', 'Tap again to stop', function () { stop(app.id); }));
            } else {
                actions.push(button('Launch', function (e) { launch(app, e.currentTarget); }, 'primary'));
            }
            if (session) actions.push(button('Logs', function () { openLogs(app.id); }));
            actions.push(button('Edit', function () { openForm(app); }));
            if (!live) actions.push(confirmButton('Delete', 'Tap again to delete', function () { remove(app.id); }));
            grid.appendChild(card({id: app.id, name: app.name, command: app.command, session: session, actions: actions}));
        });

        Object.keys(byId).forEach(function (id) {
            var session = byId[id];
            var live = session.status === 'running' || session.status === 'starting';
            var actions = [];
            if (live) actions.push(button('Resume', function () { location.href = session.url; }, 'primary'));
            actions.push(live ? confirmButton('Stop', 'Tap again to stop', function () { stop(id); }) :
                button('Dismiss', function () { stop(id); }));
            actions.push(button('Logs', function () { openLogs(id); }));
            actions.push(button('Save', function () { openForm(null, session); }));
            grid.appendChild(card({
                id: id, name: session.name, command: (session.argv || []).map(shellQuote).join(' '),
                session: session, actions: actions
            }));
        });

        empty.hidden = grid.children.length > 0;
    }

    // ---- actions ----------------------------------------------------------------

    var lastRendered = '';

    function refresh() {
        return Promise.all([api('/saved'), api('/sessions')]).then(function (r) {
            saved = r[0] || [];
            running = r[1] || [];
            // Re-render only on change: a re-render would disarm a pending
            // "tap again" confirmation.
            var state = JSON.stringify([saved, running, errors]);
            if (state === lastRendered) { retryThumbs(); return; }
            lastRendered = state;
            render();
        }).catch(function () {});
    }

    function launch(app, btn) {
        btn.disabled = true;
        btn.textContent = 'Launching…';
        delete errors[app.id];
        var body = {saved_id: app.id};
        if (app.size === 'fit') body.size = fitSize();
        api('/sessions', {method: 'POST', body: body}).then(function (session) {
            if (session.status === 'exited') {
                errors[app.id] = 'Exited at start (code ' + session.exit_code + '). See Logs.';
                refresh();
                return;
            }
            location.href = session.url;
        }).catch(function (err) {
            errors[app.id] = err.message;
            refresh();
        });
    }

    function stop(id) { api('/sessions/' + encodeURIComponent(id), {method: 'DELETE'}).finally(refresh); }
    function remove(id) { api('/saved/' + encodeURIComponent(id), {method: 'DELETE'}).finally(refresh); }

    // ---- logs ------------------------------------------------------------------

    function openLogs(id) {
        logsFor = id;
        document.getElementById('apps-logs-title').textContent = 'Logs · ' + id;
        logsText.textContent = 'Loading…';
        logsModal.hidden = false;
        loadLogs();
    }
    function loadLogs() {
        fetch('/api/apps/sessions/' + encodeURIComponent(logsFor) + '/logs?tail=200', {credentials: 'same-origin'})
            .then(function (r) { return r.ok ? r.text() : 'No logs.'; })
            .then(function (text) {
                // Without the colors an app writes to its terminal.
                logsText.textContent = text.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '') || '(empty)';
                logsText.scrollTop = logsText.scrollHeight;
            });
    }
    document.getElementById('apps-logs-refresh').addEventListener('click', loadLogs);

    // ---- form ------------------------------------------------------------------

    function syncKeysField() {
        form.querySelector('.apps-keys-field').hidden = form.elements.controls.value !== 'gamepad';
    }
    form.elements.controls.addEventListener('change', syncKeysField);

    function openForm(app, fromSession) {
        editing = app ? app.id : null;
        formError.textContent = '';
        document.getElementById('apps-form-title').textContent = app ? 'Edit ' + app.name : 'Add an app';
        var src = app || {};
        if (fromSession) {
            src = {
                name: fromSession.name,
                command: (fromSession.argv || []).map(shellQuote).join(' '),
                cwd: fromSession.cwd,
                controls: fromSession.controls,
                keys: fromSession.keys,
                gpu: 'auto',
                audio: fromSession.audio_requested || fromSession.audio,
                size: (fromSession.size || []).join('x')
            };
        }
        form.elements.name.value = src.name || '';
        form.elements.command.value = src.command || '';
        form.elements.cwd.value = src.cwd || homeDir;
        form.elements.controls.value = src.controls || 'trackpad';
        form.elements.gpu.value = src.gpu || 'auto';
        form.elements.audio.value = src.audio || 'stream';
        form.elements.keys.value = keysText(src.keys);
        var size = src.size || (touch ? 'fit' : '1280x720');
        form.querySelector('[name="size_mode"][value="' + (size === 'fit' ? 'fit' : 'fixed') + '"]').checked = true;
        form.elements.size.value = size === 'fit' ? '1280x720' : size;
        syncKeysField();
        formModal.hidden = false;
        form.elements.name.focus();
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var mode = form.querySelector('[name="size_mode"]:checked');
        var body = {
            name: form.elements.name.value,
            command: form.elements.command.value,
            cwd: form.elements.cwd.value,
            controls: form.elements.controls.value,
            gpu: form.elements.gpu.value,
            audio: form.elements.audio.value,
            keys: form.elements.controls.value === 'gamepad' ? form.elements.keys.value : '',
            size: mode && mode.value === 'fixed' ? form.elements.size.value : 'fit'
        };
        var path = editing ? '/saved/' + encodeURIComponent(editing) : '/saved';
        api(path, {method: editing ? 'PUT' : 'POST', body: body}).then(function () {
            formModal.hidden = true;
            refresh();
        }).catch(function (err) { formError.textContent = err.message; });
    });

    document.getElementById('apps-add').addEventListener('click', function () { openForm(null); });

    [formModal, logsModal].forEach(function (modal) {
        modal.addEventListener('click', function (e) {
            if (e.target.closest('[data-close]')) modal.hidden = true;
        });
    });
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        if (document.getElementById('folder-picker-modal').style.display !== 'none') return;
        formModal.hidden = true;
        logsModal.hidden = true;
    });

    if (window.FolderPicker) {
        FolderPicker.init({
            homeDir: homeDir,
            selectMode: 'folder',
            onSelect: function (path) { form.elements.cwd.value = path; }
        });
        document.getElementById('apps-browse').addEventListener('click', function () {
            FolderPicker.open(form.elements.cwd.value || homeDir);
        });
    }

    // ---- prerequisites ---------------------------------------------------------

    api('/deps').then(function (report) {
        if (!report || report.ok) return;
        var list = document.getElementById('apps-deps-list');
        report.checks.forEach(function (check) {
            if (!check.required) return;
            var li = el('li', check.ok ? 'ok' : '', (check.ok ? '✓ ' : '✗ ') + check.name + (check.ok ? '' : ' (' + check.hint + ')'));
            list.appendChild(li);
        });
        document.getElementById('apps-deps-install').textContent = report.install;
        document.getElementById('apps-deps').hidden = false;
    }).catch(function () {});

    refresh();
    setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
    window.MerlinAppsPage = {refresh: refresh, fitSize: fitSize};
})();
