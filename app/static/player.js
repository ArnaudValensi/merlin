/* Full-screen player for one app.
 *
 * Desktop: keyboard and mouse go straight to the app. Touch devices pick a
 * controls profile: Gamepad (D-pad = arrows, buttons mapped to keys),
 * Trackpad (a laptop trackpad: relative cursor, tap = click) or Touch (tap
 * where you touch). Pinch zooms the video on the client in every profile.
 * Sound starts muted (browsers want a gesture) and comes on with the first
 * touch, click or key, unless turned off in the ⋯ sheet.
 */
(function () {
    'use strict';

    var AGENT_INPUT_MS = 2000;
    var TAP_MS = 250;
    var TAP_SLOP = 10;
    var LONG_PRESS_MS = 500;
    var MAX_ZOOM = 4;

    var root = document.getElementById('player');
    var video = document.getElementById('player-video');
    var controlsEl = document.getElementById('player-controls');
    var statusEl = document.getElementById('player-status');
    var menuBtn = document.getElementById('player-menu-btn');
    var chip = document.getElementById('player-chip');
    var sheet = document.getElementById('player-sheet');
    var keysRow = document.getElementById('player-keys');
    var kb = document.getElementById('player-kb');
    var hint = document.getElementById('player-hint');
    var soundItem = sheet.querySelector('[data-action="sound"]');
    var soundState = document.getElementById('player-sound-state');
    var id = root.getAttribute('data-id');

    var fine = window.matchMedia('(pointer: fine)').matches;
    var app = null;
    var stream = null;
    var profile = null;
    var agentUntil = 0;
    var chipDetail = false;
    var wakeLock = null;
    var mods = {};          // sticky modifiers from the key row: keysym -> true
    var view = {scale: 1, x: 0, y: 0};

    function send(message) { return stream ? stream.send(message) : false; }
    function tapKey(keysym) {
        send({t: 'key', k: keysym, d: true});
        send({t: 'key', k: keysym, d: false});
    }
    function vibrate() { if (navigator.vibrate) { try { navigator.vibrate(10); } catch (e) {} } }

    // ---- status ---------------------------------------------------------------

    function status(text, actions) {
        statusEl.innerHTML = '';
        if (!text) return;
        var p = document.createElement('p');
        p.textContent = text;
        statusEl.appendChild(p);
        if (actions && actions.length) {
            var row = document.createElement('div');
            row.className = 'player-status-actions';
            actions.forEach(function (a) {
                var b = document.createElement('button');
                b.type = 'button';
                b.textContent = a.label;
                b.addEventListener('click', function (e) { e.stopPropagation(); a.run(); });
                row.appendChild(b);
            });
            statusEl.appendChild(row);
        }
    }

    var retry = {label: 'Retry', run: function () { stream.reconnect(); }};
    var toApps = {label: 'Apps', run: function () { location.href = '/apps'; }};

    function onState(state, detail) {
        updateChip();
        if (state === 'live') { status(''); acquireWakeLock(); return; }
        releaseWakeLock();
        if (state === 'connecting') status('Connecting…');
        else if (state === 'unreachable') {
            status("Can't reach " + (detail.host || 'this machine') +
                ' directly. Join the same Wi-Fi, or use Tailscale.', [retry]);
        } else if (state === 'replaced') {
            status('Opened on another device.', [{label: 'Watch here', run: function () { stream.reconnect(); }}]);
        } else if (state === 'exited') {
            var logs = {label: 'Logs', run: function () { window.open('/api/apps/sessions/' + encodeURIComponent(id) + '/logs', '_blank'); }};
            status(detail.reason === 'stopped' ? 'The app was stopped.' :
                detail.reason === 'missing' ? 'No app named ' + id + ' is running.' :
                'The app exited' + (detail.code != null ? ' (code ' + detail.code + ').' : '.'),
                detail.reason === 'exited' ? [logs, toApps] : [toApps]);
        } else if (state === 'paused') status('Paused while hidden.', [{label: 'Resume', run: function () { stream.reconnect(); }}]);
        else if (state === 'error') status(detail.message || 'Stream error.', [retry]);
        else if (state === 'closed') status('Disconnected.', [retry]);
    }

    function updateChip() {
        if (!stream) return;
        if (Date.now() < agentUntil) {
            chip.textContent = 'agent is pressing keys';
            chip.className = 'player-chip agent';
            return;
        }
        if (stream.state !== 'live') {
            chip.textContent = stream.state;
            chip.className = 'player-chip';
            return;
        }
        stream.stats().then(function (s) {
            if (!s || Date.now() < agentUntil) return;
            var text = 'LAN' + (s.rttMs != null ? ' · ' + s.rttMs + ' ms' : '');
            if (chipDetail) {
                text += (s.fps != null ? ' · ' + s.fps + ' fps' : '') +
                    (s.kbps != null ? ' · ' + (s.kbps / 1000).toFixed(1) + ' Mbit/s' : '') +
                    (s.codec ? ' · ' + s.codec : '') + (s.encoder ? ' (' + s.encoder + ')' : '');
            }
            chip.textContent = text;
            chip.className = 'player-chip live';
        });
    }
    chip.addEventListener('click', function () { chipDetail = !chipDetail; updateChip(); });

    // ---- wake lock, fullscreen, iOS hint ------------------------------------------

    function acquireWakeLock() {
        if (wakeLock || !('wakeLock' in navigator) || document.hidden) return;
        navigator.wakeLock.request('screen').then(function (lock) {
            wakeLock = lock;
            lock.addEventListener('release', function () { wakeLock = null; });
        }).catch(function () { /* insecure origin or refused: fine */ });
    }
    function releaseWakeLock() {
        if (wakeLock) { wakeLock.release().catch(function () {}); wakeLock = null; }
    }
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden && stream && stream.state === 'live') acquireWakeLock();
    });

    var isIOS = /iP(hone|od|ad)/.test(navigator.userAgent) ||
        (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    var standalone = navigator.standalone === true ||
        window.matchMedia('(display-mode: standalone)').matches;

    function goFullscreen() {
        if (fine || isIOS || document.fullscreenElement || !document.documentElement.requestFullscreen) return;
        document.documentElement.requestFullscreen({navigationUI: 'hide'}).then(function () {
            if (screen.orientation && screen.orientation.lock) {
                screen.orientation.lock('landscape').catch(function () {});
            }
        }).catch(function () {});
    }
    // Fullscreen needs a user gesture: take the first touch.
    root.addEventListener('pointerdown', function first(e) {
        if (e.pointerType === 'touch') { goFullscreen(); root.removeEventListener('pointerdown', first); }
    });

    function readFlag(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
    function writeFlag(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }

    if (isIOS && !standalone && !readFlag('app-ios-hint')) hint.hidden = false;
    document.getElementById('player-hint-close').addEventListener('click', function () {
        hint.hidden = true;
        writeFlag('app-ios-hint', '1');
    });

    // ---- zoom ---------------------------------------------------------------------

    function applyView() {
        var w = root.clientWidth, h = root.clientHeight;
        var maxX = 0, minX = w - w * view.scale, maxY = 0, minY = h - h * view.scale;
        view.x = Math.min(maxX, Math.max(minX, view.x));
        view.y = Math.min(maxY, Math.max(minY, view.y));
        video.style.transform = view.scale === 1 ? '' :
            'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.scale + ')';
    }
    function resetZoom() { view = {scale: 1, x: 0, y: 0}; applyView(); }

    /** Zoom by `factor` keeping the screen point (cx, cy) fixed. */
    function zoomAt(cx, cy, factor) {
        var next = Math.max(1, Math.min(MAX_ZOOM, view.scale * factor));
        var k = next / view.scale;
        view.x = cx - (cx - view.x) * k;
        view.y = cy - (cy - view.y) * k;
        view.scale = next;
        applyView();
    }
    function panBy(dx, dy) { view.x += dx; view.y += dy; applyView(); }
    window.addEventListener('resize', applyView);

    // ---- touch gestures on the stage ---------------------------------------------

    var touches = {};       // pointerId -> {x, y, startX, startY, t}
    var gesture = null;     // the active gesture, see onDown

    function count() { return Object.keys(touches).length; }
    function pair() {
        var ids = Object.keys(touches);
        return [touches[ids[0]], touches[ids[1]]];
    }
    function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }
    function mid(a, b) { return {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2}; }

    /** Release whatever a gesture holds (a drag's button, a long-press timer). */
    function releaseGesture(g) {
        if (!g) return;
        if (g.longTimer) clearTimeout(g.longTimer);
        if (g.holding || g.touchDrag) send({t: 'btn', b: 1, d: false});
        g.holding = false;
        g.touchDrag = false;
    }

    function onDown(e) {
        if (e.pointerType !== 'touch' || !profile) return;
        if (e.target.closest('.player-dpad, .player-pad-btn')) return;
        e.preventDefault();
        touches[e.pointerId] = {x: e.clientX, y: e.clientY, startX: e.clientX, startY: e.clientY, t: Date.now()};
        if (count() === 1) {
            var now = Date.now();
            var dragHold = profile === 'trackpad' && gesture && gesture.kind === 'tap-end' &&
                now - gesture.t < 300;
            gesture = {kind: 'one', t: now, moved: false, dragHold: dragHold, longTimer: null};
            if (profile === 'touch') {
                gesture.longTimer = setTimeout(function () {
                    if (gesture && gesture.kind === 'one' && !gesture.moved) {
                        clickAt(e.clientX, e.clientY, 3);
                        gesture.kind = 'done';
                        vibrate();
                    }
                }, LONG_PRESS_MS);
            }
            if (dragHold) { send({t: 'btn', b: 1, d: true}); gesture.holding = true; }
        } else if (count() === 2) {
            releaseGesture(gesture);
            var p = pair();
            gesture = {kind: 'two', t: Date.now(), startDist: dist(p[0], p[1]), lastDist: dist(p[0], p[1]),
                lastMid: mid(p[0], p[1]), mode: null, scrollAcc: 0, moved: false};
        }
    }

    function onMove(e) {
        var t = touches[e.pointerId];
        if (!t || !gesture) return;
        e.preventDefault();
        var dx = e.clientX - t.x, dy = e.clientY - t.y;
        t.x = e.clientX; t.y = e.clientY;
        if (gesture.kind === 'one') {
            if (!gesture.moved && Math.hypot(t.x - t.startX, t.y - t.startY) < TAP_SLOP) return;
            if (!gesture.moved && gesture.longTimer) clearTimeout(gesture.longTimer);
            if (profile === 'trackpad') {
                gesture.moved = true;
                var speed = trackpadSpeed();
                send({t: 'rel', dx: Math.round(dx * speed), dy: Math.round(dy * speed)});
            } else if (profile === 'touch') {
                if (!gesture.moved) {
                    var start = MerlinApps.toDisplay(video, t.startX, t.startY);
                    if (start) { send({t: 'move', x: start.x, y: start.y}); send({t: 'btn', b: 1, d: true}); gesture.touchDrag = true; }
                }
                gesture.moved = true;
                var point = MerlinApps.toDisplay(video, t.x, t.y);
                if (point) send({t: 'move', x: point.x, y: point.y});
            } else {
                gesture.moved = true;  // gamepad: one finger on the stage does nothing
            }
        } else if (gesture.kind === 'two' && count() === 2) {
            var p = pair();
            var d = dist(p[0], p[1]), m = mid(p[0], p[1]);
            gesture.moved = true;
            if (!gesture.mode) {
                if (Math.abs(d - gesture.startDist) > 24) gesture.mode = 'pinch';
                else if (Math.hypot(m.x - gesture.lastMid.x, m.y - gesture.lastMid.y) > 12) {
                    gesture.mode = profile === 'trackpad' && view.scale === 1 ? 'scroll' : 'pan';
                } else return;
            }
            if (gesture.mode === 'pinch') {
                zoomAt(m.x, m.y, d / gesture.lastDist);
                panBy(m.x - gesture.lastMid.x, m.y - gesture.lastMid.y);
            } else if (gesture.mode === 'pan') {
                panBy(m.x - gesture.lastMid.x, m.y - gesture.lastMid.y);
            } else if (gesture.mode === 'scroll') {
                gesture.scrollAcc += m.y - gesture.lastMid.y;
                while (Math.abs(gesture.scrollAcc) >= 30) {
                    var step = gesture.scrollAcc > 0 ? -1 : 1;  // natural scrolling
                    send({t: 'wheel', dy: step});
                    gesture.scrollAcc += step * 30;
                }
            }
            gesture.lastDist = d;
            gesture.lastMid = m;
        }
    }

    function onUp(e) {
        var t = touches[e.pointerId];
        if (!t) return;
        e.preventDefault();
        delete touches[e.pointerId];
        if (!gesture) return;
        if (gesture.longTimer) clearTimeout(gesture.longTimer);
        if (gesture.kind === 'one' && count() === 0) {
            var quick = Date.now() - gesture.t < TAP_MS;
            if (profile === 'trackpad') {
                if (gesture.holding) send({t: 'btn', b: 1, d: false});
                else if (!gesture.moved && quick) {
                    send({t: 'btn', b: 1, d: true});
                    send({t: 'btn', b: 1, d: false});
                    gesture = {kind: 'tap-end', t: Date.now()};
                    return;
                }
            } else if (profile === 'touch') {
                if (gesture.touchDrag) send({t: 'btn', b: 1, d: false});
                else if (!gesture.moved) clickAt(t.startX, t.startY, 1);
            }
            gesture = null;
        } else if (gesture.kind === 'two') {
            if (count() === 0) {
                if (profile === 'trackpad' && !gesture.moved && Date.now() - gesture.t < TAP_MS) {
                    send({t: 'btn', b: 3, d: true});
                    send({t: 'btn', b: 3, d: false});
                }
                gesture = null;
            }
        } else if (count() === 0) {
            gesture = null;
        }
    }

    function clickAt(x, y, button) {
        var point = MerlinApps.toDisplay(video, x, y);
        if (!point) return;
        send({t: 'move', x: point.x, y: point.y});
        send({t: 'btn', b: button, d: true});
        send({t: 'btn', b: button, d: false});
    }

    /** Trackpad speed: display pixels per finger pixel, a bit faster than 1:1. */
    function trackpadSpeed() {
        var rect = video.getBoundingClientRect();
        var shown = Math.min(rect.width / (video.videoWidth || 1), rect.height / (video.videoHeight || 1));
        return 1.6 / (shown || 1);
    }

    root.addEventListener('pointerdown', onDown);
    root.addEventListener('pointermove', onMove);
    root.addEventListener('pointerup', onUp);
    root.addEventListener('pointercancel', onUp);
    root.addEventListener('contextmenu', function (e) { e.preventDefault(); });

    // ---- gamepad ------------------------------------------------------------------

    var FACE_POSITIONS = {Y: 'top', X: 'left', B: 'right', A: 'bottom'};

    function buildGamepad() {
        controlsEl.innerHTML = '';
        var dpad = document.createElement('div');
        dpad.className = 'player-dpad';
        ['Up', 'Down', 'Left', 'Right'].forEach(function (dir) {
            var s = document.createElement('span');
            s.setAttribute('data-dir', dir);
            // SVG, not ▲◀ glyphs: phones render those as colored emoji.
            var rotate = {Up: 0, Right: 90, Down: 180, Left: 270}[dir];
            s.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" ' +
                'style="transform: rotate(' + rotate + 'deg)"><path d="M12 5l8 12H4z"/></svg>';
            dpad.appendChild(s);
        });
        controlsEl.appendChild(dpad);
        bindDpad(dpad);

        var keys = (app && app.keys && Object.keys(app.keys).length) ? app.keys : {A: 'Return', B: 'Escape'};
        var face = document.createElement('div');
        face.className = 'player-pad-face';
        var meta = document.createElement('div');
        meta.className = 'player-pad-meta';
        var freePositions = ['bottom', 'right', 'left', 'top'].filter(function (pos) {
            return !Object.keys(keys).some(function (name) { return FACE_POSITIONS[name] === pos; });
        });
        Object.keys(keys).forEach(function (name) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'player-pad-btn';
            b.setAttribute('data-button', name);
            b.innerHTML = '<span></span><small></small>';
            b.firstChild.textContent = name;
            b.lastChild.textContent = keys[name];
            var pos = FACE_POSITIONS[name] || (name.length <= 2 ? freePositions.shift() : null);
            if (pos) { b.setAttribute('data-pos', pos); face.appendChild(b); }
            else meta.appendChild(b);
            bindPadButton(b, keys[name]);
        });
        controlsEl.appendChild(face);
        if (meta.children.length) controlsEl.appendChild(meta);
    }

    // Release functions of every gamepad control that can hold a key: the
    // controls are rebuilt on a profile change, never with a key left down.
    var padReleases = [];
    function releasePad() {
        padReleases.forEach(function (release) { release(); });
        padReleases = [];
    }

    function bindPadButton(el, keysym) {
        var pointer = null;
        el.addEventListener('pointerdown', function (e) {
            e.preventDefault();
            e.stopPropagation();
            if (pointer !== null) return;
            pointer = e.pointerId;
            try { el.setPointerCapture(e.pointerId); } catch (x) {}
            el.classList.add('on');
            send({t: 'key', k: keysym, d: true});
            vibrate();
        });
        function release() {
            if (pointer === null) return;
            pointer = null;
            el.classList.remove('on');
            send({t: 'key', k: keysym, d: false});
        }
        function up(e) { if (e.pointerId === pointer) release(); }
        el.addEventListener('pointerup', up);
        el.addEventListener('pointercancel', up);
        el.addEventListener('lostpointercapture', up);
        padReleases.push(release);
    }

    function bindDpad(el) {
        var pointer = null, current = null;
        function setDir(dir) {
            if (dir === current) return;
            if (current) {
                send({t: 'key', k: current, d: false});
                el.querySelector('[data-dir="' + current + '"]').classList.remove('on');
            }
            current = dir;
            if (dir) {
                send({t: 'key', k: dir, d: true});
                el.querySelector('[data-dir="' + dir + '"]').classList.add('on');
                vibrate();
            }
        }
        function dirAt(e) {
            var r = el.getBoundingClientRect();
            var dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
            if (Math.hypot(dx, dy) < r.width * 0.12) return null;  // dead zone
            return Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'Right' : 'Left') : (dy > 0 ? 'Down' : 'Up');
        }
        el.addEventListener('pointerdown', function (e) {
            e.preventDefault();
            e.stopPropagation();
            if (pointer !== null) return;
            pointer = e.pointerId;
            try { el.setPointerCapture(e.pointerId); } catch (x) {}
            setDir(dirAt(e));
        });
        el.addEventListener('pointermove', function (e) {
            if (e.pointerId === pointer) setDir(dirAt(e));
        });
        function release() {
            pointer = null;
            setDir(null);
        }
        function up(e) { if (e.pointerId === pointer) release(); }
        el.addEventListener('pointerup', up);
        el.addEventListener('pointercancel', up);
        el.addEventListener('lostpointercapture', up);
        padReleases.push(release);
    }

    // ---- profiles -------------------------------------------------------------------

    function setProfile(next) {
        releasePad();
        releaseGesture(gesture);
        gesture = null;
        touches = {};
        profile = next;
        writeFlag('app-controls:' + id, next);
        controlsEl.innerHTML = '';
        if (next === 'gamepad') buildGamepad();
        sheet.querySelectorAll('[data-controls]').forEach(function (b) {
            b.setAttribute('aria-checked', b.getAttribute('data-controls') === next ? 'true' : 'false');
        });
        root.setAttribute('data-controls', next);
    }

    function defaultProfile() {
        var saved = readFlag('app-controls:' + id);
        if (saved === 'gamepad' || saved === 'trackpad' || saved === 'touch') return saved;
        if (app && app.controls) return app.controls;
        return app && app.keys && Object.keys(app.keys).length ? 'gamepad' : 'trackpad';
    }

    // ---- sound ------------------------------------------------------------------------

    var sound = MerlinApps.sound(video, 'player', window);
    function renderSound() {
        var on = !video.muted;
        soundState.textContent = on ? 'On' : 'Off';
        soundItem.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    video.addEventListener('volumechange', renderSound);
    renderSound();

    // ---- sheet -----------------------------------------------------------------------

    function openSheet() { sheet.hidden = false; menuBtn.setAttribute('aria-expanded', 'true'); }
    function closeSheet() {
        sheet.hidden = true;
        menuBtn.setAttribute('aria-expanded', 'false');
        var stop = sheet.querySelector('[data-action="stop"]');
        stop.classList.remove('confirm');
        stop.textContent = 'Stop app';
    }
    menuBtn.addEventListener('click', function () { sheet.hidden ? openSheet() : closeSheet(); });
    root.addEventListener('pointerdown', function () { if (!sheet.hidden) closeSheet(); }, true);

    sheet.addEventListener('click', function (e) {
        var b = e.target.closest('button');
        if (!b) return;
        var controls = b.getAttribute('data-controls');
        if (controls) { setProfile(controls); return; }
        var action = b.getAttribute('data-action');
        if (action === 'sound') { sound.set(!sound.on); return; }
        if (action === 'keyboard') { closeSheet(); openKeyboard(); }
        else if (action === 'reset-zoom') { resetZoom(); closeSheet(); }
        else if (action === 'screenshot') { downloadScreenshot(); closeSheet(); }
        else if (action === 'leave') leave();
        else if (action === 'stop') {
            if (!b.classList.contains('confirm')) {
                b.classList.add('confirm');
                b.textContent = 'Tap again to stop ' + (app ? app.name : id);
                return;
            }
            closeSheet();
            fetch('/api/apps/sessions/' + encodeURIComponent(id), {method: 'DELETE', credentials: 'same-origin'})
                .catch(function () {});
        }
    });

    function downloadScreenshot() {
        var a = document.createElement('a');
        a.href = '/api/apps/sessions/' + encodeURIComponent(id) + '/screenshot';
        a.download = id + '-screenshot.png';
        document.body.appendChild(a);
        a.click();
        a.remove();
    }

    function leave() {
        if (stream) stream.destroy();
        if (document.fullscreenElement) document.exitFullscreen().catch(function () {});
        var ref = document.referrer;
        if (ref && ref.indexOf(location.origin) === 0 && history.length > 1) history.back();
        else location.href = '/apps';
    }

    // ---- keyboard (phone) ---------------------------------------------------------------

    function withMods(fn) {
        var held = Object.keys(mods);
        held.forEach(function (m) { send({t: 'key', k: m, d: true}); });
        fn();
        held.forEach(function (m) { send({t: 'key', k: m, d: false}); });
        if (held.length) {
            mods = {};
            keysRow.querySelectorAll('[data-mod]').forEach(function (b) { b.classList.remove('on'); });
        }
    }

    function openKeyboard() {
        keysRow.hidden = false;
        placeKeysRow();
        kb.value = '';
        kb.focus({preventScroll: true});
    }
    function closeKeyboard() { keysRow.hidden = true; kb.blur(); }

    function placeKeysRow() {
        var vv = window.visualViewport;
        var offset = vv ? Math.max(0, window.innerHeight - vv.height - vv.offsetTop) : 0;
        keysRow.style.bottom = offset + 'px';
    }
    if (window.visualViewport) window.visualViewport.addEventListener('resize', placeKeysRow);

    keysRow.addEventListener('pointerdown', function (e) { e.preventDefault(); });  // keep kb focus
    keysRow.addEventListener('click', function (e) {
        var b = e.target.closest('button');
        if (!b) return;
        if (b.getAttribute('data-action') === 'keyboard-close') { closeKeyboard(); return; }
        var mod = b.getAttribute('data-mod');
        if (mod) {
            if (mods[mod]) delete mods[mod]; else mods[mod] = true;
            b.classList.toggle('on', !!mods[mod]);
            return;
        }
        var key = b.getAttribute('data-key');
        if (key) withMods(function () { tapKey(key); });
    });

    kb.addEventListener('beforeinput', function (e) {
        if (e.inputType === 'deleteContentBackward') { e.preventDefault(); withMods(function () { tapKey('BackSpace'); }); }
        else if (e.inputType === 'insertLineBreak') { e.preventDefault(); withMods(function () { tapKey('Return'); }); }
    });
    kb.addEventListener('input', function (e) {
        var text = e.data != null ? e.data : kb.value;
        kb.value = '';
        if (!text) return;
        if (Object.keys(mods).length && text.length === 1) {
            var keysym = MerlinApps.keysymFor({key: text, code: ''}) || text;
            withMods(function () { tapKey(keysym); });
        } else {
            send({t: 'text', s: text});
        }
    });
    kb.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); withMods(function () { tapKey('Return'); }); }
    });

    // ---- start ------------------------------------------------------------------------

    stream = MerlinApps.connect({
        id: id,
        video: video,
        onState: onState,
        onWelcome: function (info) {
            app = info.app;
            if (app) {
                document.getElementById('player-title').textContent = app.name;
                document.title = app.name + ' · apps';
            }
            if (!fine && !profile) setProfile(defaultProfile());
        },
        onReady: function (info) { soundItem.hidden = !info.audio; },
        onAgentInput: function () { agentUntil = Date.now() + AGENT_INPUT_MS; updateChip(); }
    });
    // Root (re)binds after a reconnect via getStream, so a single binding is enough.
    if (fine) MerlinApps.bindDesktopInput(root, video, function () { return stream; });
    root.focus({preventScroll: true});
    setInterval(updateChip, 1000);

    window.MerlinPlayer = {
        get stream() { return stream; },
        get profile() { return profile; },
        setProfile: setProfile,
        get sound() { return sound; },
        get zoom() { return view.scale; }
    };
})();
