/* App stream client: WebRTC over the local network, signaling over Merlin's
 * WebSocket. Shared by the full-screen player, the terminal panel and the
 * mobile mini-player.
 *
 *   const stream = MerlinApps.connect({id, video, onState, onWelcome, ...});
 *   stream.send({t: 'key', k: 'Right', d: true});
 *   stream.close();
 *
 * States: connecting, live, unreachable (ICE failed or timed out: not on the
 * same network), replaced (opened on another device), exited (the app ended
 * or was stopped), paused (hidden for a while), error, closed.
 */
(function () {
    'use strict';

    var CONNECT_TIMEOUT_MS = 8000;
    var HIDDEN_CLOSE_MS = 30000;

    // Printable characters that are not their own X keysym name.
    var CHAR_KEYSYMS = {
        ' ': 'space', '!': 'exclam', '"': 'quotedbl', '#': 'numbersign',
        '$': 'dollar', '%': 'percent', '&': 'ampersand', "'": 'apostrophe',
        '(': 'parenleft', ')': 'parenright', '*': 'asterisk', '+': 'plus',
        ',': 'comma', '-': 'minus', '.': 'period', '/': 'slash', ':': 'colon',
        ';': 'semicolon', '<': 'less', '=': 'equal', '>': 'greater',
        '?': 'question', '@': 'at', '[': 'bracketleft', '\\': 'backslash',
        ']': 'bracketright', '^': 'asciicircum', '_': 'underscore', '`': 'grave',
        '{': 'braceleft', '|': 'bar', '}': 'braceright', '~': 'asciitilde'
    };

    // KeyboardEvent.key names -> X keysym names.
    var NAMED_KEYSYMS = {
        ArrowLeft: 'Left', ArrowRight: 'Right', ArrowUp: 'Up', ArrowDown: 'Down',
        Enter: 'Return', Escape: 'Escape', Tab: 'Tab', Backspace: 'BackSpace',
        Delete: 'Delete', Insert: 'Insert', Home: 'Home', End: 'End',
        PageUp: 'Prior', PageDown: 'Next', Shift: 'Shift_L', Control: 'Control_L',
        Alt: 'Alt_L', AltGraph: 'ISO_Level3_Shift', Meta: 'Super_L',
        CapsLock: 'Caps_Lock', ContextMenu: 'Menu', ' ': 'space'
    };

    /** X keysym name for a KeyboardEvent, or null (send it as text instead). */
    function keysymFor(event) {
        var key = event.key;
        if (NAMED_KEYSYMS[key]) {
            if (key === 'Shift' && event.code === 'ShiftRight') return 'Shift_R';
            if (key === 'Control' && event.code === 'ControlRight') return 'Control_R';
            return NAMED_KEYSYMS[key];
        }
        if (/^F([1-9]|1[0-9]|2[0-4])$/.test(key)) return key;
        if (key.length === 1) {
            if (/[A-Za-z0-9]/.test(key)) return key;
            if (CHAR_KEYSYMS[key]) return CHAR_KEYSYMS[key];
        }
        return null;
    }

    /** Client point -> display pixels, for a video shown with object-fit: contain. */
    function toDisplay(video, clientX, clientY) {
        var vw = video.videoWidth, vh = video.videoHeight;
        if (!vw || !vh) return null;
        var rect = video.getBoundingClientRect();
        var scale = Math.min(rect.width / vw, rect.height / vh);
        var left = rect.left + (rect.width - vw * scale) / 2;
        var top = rect.top + (rect.height - vh * scale) / 2;
        var x = Math.round((clientX - left) / scale);
        var y = Math.round((clientY - top) / scale);
        if (x < 0 || y < 0 || x >= vw || y >= vh) return null;
        return {x: x, y: y};
    }

    function wsUrl(id) {
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        return proto + '//' + location.host + '/ws/apps/' + encodeURIComponent(id) + '/stream';
    }

    function browserCodecs() {
        var out = [];
        try {
            var caps = RTCRtpReceiver.getCapabilities('video');
            (caps && caps.codecs || []).forEach(function (codec) {
                var name = String(codec.mimeType || '').split('/')[1];
                if (name && out.indexOf(name.toUpperCase()) === -1) out.push(name.toUpperCase());
            });
        } catch (e) { /* older browsers: let the server pick VP8 */ }
        return out.length ? out : ['VP8'];
    }

    function connect(opts) {
        var video = opts.video;
        var ws = null, pc = null, channel = null;
        var state = 'closed';
        var timer = null, hiddenTimer = null, retryTimer = null;
        var lastBytes = null;
        var info = {host: '', app: null, encoder: '', codec: ''};
        // Every open() and teardown() starts a new generation. Callbacks of an
        // older connection (a late promise, a closing socket) check theirs and
        // do nothing, and nothing runs at all once the client is destroyed.
        var gen = 0;
        var destroyed = false;

        // A streamer that dies gets one automatic retry (per 20 s); a lost
        // server (a Merlin restart) gets a few, with backoff, before giving up.
        var lastAutoRetry = 0;
        var reconnects = 0;
        var RECONNECT_DELAYS = [1000, 2000, 3000, 4000, 5000, 5000, 5000];

        function current(g) { return !destroyed && g === gen; }

        function setState(next, detail) {
            if (destroyed) return;
            if (state === next && !detail) return;
            state = next;
            if (video && video.parentElement) video.parentElement.setAttribute('data-stream-state', next);
            if (opts.onState) opts.onState(next, detail || {});
        }

        function teardown() {
            gen++;
            clearTimeout(timer);
            if (channel) { try { channel.close(); } catch (e) {} channel = null; }
            if (pc) { try { pc.close(); } catch (e) {} pc = null; }
            if (ws) {
                ws.onclose = null;
                ws.onmessage = null;
                try { ws.close(); } catch (e) {}
                ws = null;
            }
        }

        function schedule(fn, delay) {
            clearTimeout(retryTimer);
            retryTimer = setTimeout(function () { if (!destroyed) fn(); }, delay);
        }

        function onOffer(sdp, g, socket) {
            var peer = new RTCPeerConnection({iceServers: []});
            pc = peer;
            function sendOn(message) {
                if (current(g) && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
            }
            peer.ontrack = function (event) {
                if (!current(g)) return;
                var stream = event.streams && event.streams[0];
                if (!stream) stream = new MediaStream([event.track]);
                if (video.srcObject !== stream) video.srcObject = stream;
                var playing = video.play();
                if (playing && playing.catch) playing.catch(function () {});
            };
            peer.ondatachannel = function (event) { if (current(g)) channel = event.channel; };
            peer.onicecandidate = function (event) {
                if (event.candidate) {
                    sendOn({type: 'ice', candidate: event.candidate.candidate,
                            sdpMLineIndex: event.candidate.sdpMLineIndex});
                }
            };
            peer.onconnectionstatechange = function () {
                if (!current(g)) return;
                if (peer.connectionState === 'connected') {
                    clearTimeout(timer);
                    reconnects = 0;
                    setState('live');
                } else if (peer.connectionState === 'failed') {
                    unreachable();
                }
            };
            peer.setRemoteDescription({type: 'offer', sdp: sdp})
                .then(function () { return peer.createAnswer(); })
                .then(function (answer) {
                    return peer.setLocalDescription(answer).then(function () {
                        sendOn({type: 'answer', sdp: answer.sdp});
                    });
                })
                .catch(function (err) {
                    if (current(g)) fail('Could not start the stream: ' + err);
                });
        }

        function unreachable() {
            teardown();
            setState('unreachable', {host: info.host});
        }

        function fail(message) {
            teardown();
            if (destroyed) return;
            if (Date.now() - lastAutoRetry > 20000) {
                lastAutoRetry = Date.now();
                setState('connecting');
                schedule(open, 1000);
                return;
            }
            setState('error', {message: message});
        }

        function onMessage(msg, g, socket) {
            switch (msg.type) {
                case 'welcome':
                    info.host = msg.host || '';
                    info.app = msg.app || null;
                    if (opts.onWelcome) opts.onWelcome(info);
                    socket.send(JSON.stringify({type: 'hello', codecs: browserCodecs()}));
                    break;
                case 'ready':
                    info.encoder = msg.encoder || '';
                    info.codec = msg.codec || '';
                    if (video && video.parentElement) {
                        video.parentElement.setAttribute('data-encoder', info.encoder);
                        video.parentElement.setAttribute('data-codec', info.codec);
                    }
                    if (opts.onReady) opts.onReady(info);
                    break;
                case 'offer':
                    onOffer(msg.sdp, g, socket);
                    break;
                case 'ice':
                    if (pc && msg.candidate) {
                        pc.addIceCandidate({candidate: msg.candidate, sdpMLineIndex: msg.sdpMLineIndex})
                            .catch(function () {});
                    }
                    break;
                case 'agent_input':
                    if (opts.onAgentInput) opts.onAgentInput(msg.at);
                    break;
                case 'replaced':
                    teardown();
                    setState('replaced');
                    break;
                case 'exited':
                    teardown();
                    setState('exited', {reason: msg.reason, code: msg.code});
                    break;
                case 'error':
                    fail(msg.message || 'Stream error');
                    break;
            }
        }

        function open() {
            if (destroyed) return;
            teardown();
            clearTimeout(retryTimer);
            var g = gen;
            lastBytes = null;
            setState('connecting');
            var socket = new WebSocket(wsUrl(opts.id));
            ws = socket;
            socket.onmessage = function (event) {
                if (!current(g)) return;
                var msg;
                try { msg = JSON.parse(event.data); } catch (e) { return; }
                onMessage(msg, g, socket);
            };
            socket.onclose = function (event) {
                if (!current(g)) return;
                ws = null;
                teardown();
                if (event.code === 4401) { setState('error', {message: 'Not signed in'}); return; }
                if (reconnects < RECONNECT_DELAYS.length) {
                    setState('connecting');
                    schedule(open, RECONNECT_DELAYS[reconnects++]);
                    return;
                }
                setState('closed');
            };
            timer = setTimeout(function () {
                if (current(g) && state === 'connecting') unreachable();
            }, CONNECT_TIMEOUT_MS);
            reconcileHidden();
        }

        function close() {
            clearTimeout(retryTimer);
            teardown();
            setState('closed');
        }

        // ---- hidden pages pause the stream; picture-in-picture still watches ----

        function inPip() { return !!video && document.pictureInPictureElement === video; }

        function pause() {
            clearTimeout(retryTimer);
            if (state === 'live' || state === 'connecting') {
                teardown();
                setState('paused');
            }
        }

        /** Arm the pause timer when the stream is truly unwatched, else disarm. */
        function reconcileHidden() {
            if (destroyed || opts.pauseWhenHidden === false) return;
            if (document.hidden && !inPip()) {
                if (!hiddenTimer) {
                    hiddenTimer = setTimeout(function () {
                        hiddenTimer = null;
                        if (document.hidden && !inPip()) pause();
                    }, HIDDEN_CLOSE_MS);
                }
            } else {
                clearTimeout(hiddenTimer);
                hiddenTimer = null;
                if (!document.hidden && state === 'paused') open();
            }
        }

        if (opts.pauseWhenHidden !== false) {
            document.addEventListener('visibilitychange', reconcileHidden);
            if (video) {
                video.addEventListener('enterpictureinpicture', reconcileHidden);
                video.addEventListener('leavepictureinpicture', reconcileHidden);
            }
        }

        function stats() {
            var peer = pc;
            if (!peer) return Promise.resolve(null);
            return peer.getStats().then(function (report) {
                var out = {rttMs: null, fps: null, kbps: null, codec: info.codec, encoder: info.encoder};
                var codecs = {};
                report.forEach(function (s) { if (s.type === 'codec') codecs[s.id] = s; });
                report.forEach(function (s) {
                    if (s.type === 'candidate-pair' && s.nominated && s.state === 'succeeded' &&
                        s.currentRoundTripTime != null) {
                        out.rttMs = Math.round(s.currentRoundTripTime * 1000);
                    }
                    if (s.type === 'inbound-rtp' && s.kind === 'video') {
                        if (s.framesPerSecond != null) out.fps = Math.round(s.framesPerSecond);
                        var now = s.timestamp, bytes = s.bytesReceived;
                        if (lastBytes) {
                            var dt = (now - lastBytes.t) / 1000;
                            if (dt > 0) out.kbps = Math.round((bytes - lastBytes.b) * 8 / 1000 / dt);
                        }
                        lastBytes = {t: now, b: bytes};
                        if (s.codecId && codecs[s.codecId]) {
                            out.codec = String(codecs[s.codecId].mimeType || '').split('/')[1] || out.codec;
                        }
                    }
                });
                return out;
            });
        }

        open();

        return {
            send: function (message) {
                if (channel && channel.readyState === 'open') {
                    channel.send(JSON.stringify(message));
                    return true;
                }
                return false;
            },
            close: close,
            reconnect: function () { reconnects = 0; open(); },
            stats: stats,
            get state() { return state; },
            get info() { return info; },
            destroy: function () {
                if (destroyed) return;
                document.removeEventListener('visibilitychange', reconcileHidden);
                if (video) {
                    video.removeEventListener('enterpictureinpicture', reconcileHidden);
                    video.removeEventListener('leavepictureinpicture', reconcileHidden);
                }
                clearTimeout(hiddenTimer);
                clearTimeout(retryTimer);
                close();
                destroyed = true;
            }
        };
    }

    /** Desktop keyboard and mouse on a focusable element wrapping the video. */
    function bindDesktopInput(target, video, getStream) {
        var held = {};      // KeyboardEvent.code -> keysym sent on keydown
        var pendingMove = null, moveQueued = false;

        function stream() { return getStream(); }

        target.addEventListener('keydown', function (event) {
            if (event.metaKey && event.key !== 'Meta') return; // leave OS shortcuts alone
            var keysym = keysymFor(event);
            event.preventDefault();
            var s = stream();
            if (!s) return;
            if (!keysym) {
                if (event.key.length === 1) s.send({t: 'text', s: event.key});
                return;
            }
            if (event.repeat && held[event.code] === keysym) {
                s.send({t: 'key', k: keysym, d: true});
                return;
            }
            held[event.code] = keysym;
            s.send({t: 'key', k: keysym, d: true});
        });
        target.addEventListener('keyup', function (event) {
            var keysym = held[event.code] || keysymFor(event);
            delete held[event.code];
            event.preventDefault();
            var s = stream();
            if (s && keysym) s.send({t: 'key', k: keysym, d: false});
        });
        target.addEventListener('blur', function () {
            var s = stream();
            Object.keys(held).forEach(function (code) {
                if (s) s.send({t: 'key', k: held[code], d: false});
            });
            held = {};
            releaseButtons();
        });

        function flushMove() {
            moveQueued = false;
            var s = stream();
            if (s && pendingMove) s.send({t: 'move', x: pendingMove.x, y: pendingMove.y});
            pendingMove = null;
        }
        target.addEventListener('pointermove', function (event) {
            if (event.pointerType === 'touch') return;
            var point = toDisplay(video, event.clientX, event.clientY);
            if (!point) return;
            pendingMove = point;
            if (!moveQueued) { moveQueued = true; requestAnimationFrame(flushMove); }
        });
        // Buttons: pointer capture keeps the release coming to us when the drag
        // ends outside the video (over the terminal); whatever is still held
        // when capture or focus is lost is released, never left pressed.
        var buttonsHeld = {};
        function releaseButtons() {
            var s = stream();
            Object.keys(buttonsHeld).forEach(function (b) {
                if (s) s.send({t: 'btn', b: +b, d: false});
            });
            buttonsHeld = {};
        }
        // Overlays on the video (Retry, Logs) keep their own clicks: capturing
        // the pointer would retarget the click away from them.
        function onControl(event) {
            return !!(event.target && event.target.closest &&
                event.target.closest('button, a, input, select, textarea, [role="button"]'));
        }
        target.addEventListener('pointerdown', function (event) {
            if (event.pointerType === 'touch' || onControl(event)) return;
            var point = toDisplay(video, event.clientX, event.clientY);
            var s = stream();
            target.focus({preventScroll: true});
            event.preventDefault();
            if (!s || !point) return;
            try { target.setPointerCapture(event.pointerId); } catch (e) {}
            var b = event.button + 1;
            s.send({t: 'move', x: point.x, y: point.y});
            s.send({t: 'btn', b: b, d: true});
            buttonsHeld[b] = true;
        });
        target.addEventListener('pointerup', function (event) {
            if (event.pointerType === 'touch') return;
            var b = event.button + 1;
            if (!buttonsHeld[b]) return;
            delete buttonsHeld[b];
            var s = stream();
            var point = toDisplay(video, event.clientX, event.clientY);
            if (s && point) s.send({t: 'move', x: point.x, y: point.y});
            if (s) s.send({t: 'btn', b: b, d: false});
            event.preventDefault();
        });
        target.addEventListener('lostpointercapture', releaseButtons);
        target.addEventListener('pointercancel', releaseButtons);
        target.addEventListener('contextmenu', function (event) { event.preventDefault(); });
        target.addEventListener('wheel', function (event) {
            var s = stream();
            if (!s) return;
            event.preventDefault();
            var notches = Math.max(-5, Math.min(5, Math.round(event.deltaY / 50))) ||
                (event.deltaY < 0 ? -1 : 1);
            s.send({t: 'wheel', dy: notches});
        }, {passive: false});
    }

    window.MerlinApps = {
        connect: connect,
        toDisplay: toDisplay,
        keysymFor: keysymFor,
        bindDesktopInput: bindDesktopInput
    };
})();
