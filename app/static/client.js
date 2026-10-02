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
        var timer = null, hiddenTimer = null;
        var closedByUs = false;
        var lastBytes = null;
        var info = {host: '', app: null, encoder: '', codec: ''};

        function setState(next, detail) {
            if (state === next && !detail) return;
            state = next;
            if (video && video.parentElement) video.parentElement.setAttribute('data-stream-state', next);
            if (opts.onState) opts.onState(next, detail || {});
        }

        function teardown() {
            clearTimeout(timer);
            if (channel) { try { channel.close(); } catch (e) {} channel = null; }
            if (pc) { try { pc.close(); } catch (e) {} pc = null; }
            if (ws) {
                ws.onclose = null;
                try { ws.close(); } catch (e) {}
                ws = null;
            }
        }

        function send(message) {
            if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
        }

        function onOffer(sdp) {
            pc = new RTCPeerConnection({iceServers: []});
            pc.ontrack = function (event) {
                var stream = event.streams && event.streams[0];
                if (!stream) stream = new MediaStream([event.track]);
                if (video.srcObject !== stream) video.srcObject = stream;
                var playing = video.play();
                if (playing && playing.catch) playing.catch(function () {});
            };
            pc.ondatachannel = function (event) { channel = event.channel; };
            pc.onicecandidate = function (event) {
                if (event.candidate) {
                    send({type: 'ice', candidate: event.candidate.candidate,
                          sdpMLineIndex: event.candidate.sdpMLineIndex});
                }
            };
            pc.onconnectionstatechange = function () {
                if (!pc) return;
                if (pc.connectionState === 'connected') {
                    clearTimeout(timer);
                    reconnects = 0;
                    setState('live');
                } else if (pc.connectionState === 'failed') {
                    unreachable();
                }
            };
            pc.setRemoteDescription({type: 'offer', sdp: sdp})
                .then(function () { return pc.createAnswer(); })
                .then(function (answer) {
                    return pc.setLocalDescription(answer).then(function () {
                        send({type: 'answer', sdp: answer.sdp});
                    });
                })
                .catch(function (err) { fail('Could not start the stream: ' + err); });
        }

        function unreachable() {
            closedByUs = true;
            teardown();
            setState('unreachable', {host: info.host});
        }

        // A streamer that dies gets one automatic retry (per 20 s); a lost
        // server (a Merlin restart) gets a few, with backoff, before giving up.
        var lastAutoRetry = 0;
        var reconnects = 0;
        var retryTimer = null;
        var RECONNECT_DELAYS = [1000, 2000, 3000, 4000, 5000, 5000, 5000];

        function fail(message) {
            closedByUs = true;
            teardown();
            if (Date.now() - lastAutoRetry > 20000) {
                lastAutoRetry = Date.now();
                setState('connecting');
                clearTimeout(retryTimer);
                retryTimer = setTimeout(open, 1000);
                return;
            }
            setState('error', {message: message});
        }

        function onMessage(event) {
            var msg;
            try { msg = JSON.parse(event.data); } catch (e) { return; }
            switch (msg.type) {
                case 'welcome':
                    info.host = msg.host || '';
                    info.app = msg.app || null;
                    if (opts.onWelcome) opts.onWelcome(info);
                    send({type: 'hello', codecs: browserCodecs()});
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
                    onOffer(msg.sdp);
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
                    closedByUs = true;
                    teardown();
                    setState('replaced');
                    break;
                case 'exited':
                    closedByUs = true;
                    teardown();
                    setState('exited', {reason: msg.reason, code: msg.code});
                    break;
                case 'error':
                    fail(msg.message || 'Stream error');
                    break;
            }
        }

        function open() {
            teardown();
            closedByUs = false;
            lastBytes = null;
            setState('connecting');
            ws = new WebSocket(wsUrl(opts.id));
            ws.onmessage = onMessage;
            ws.onclose = function (event) {
                ws = null;
                if (closedByUs) return;
                teardown();
                if (event.code === 4401) { setState('error', {message: 'Not signed in'}); return; }
                if (reconnects < RECONNECT_DELAYS.length) {
                    setState('connecting');
                    clearTimeout(retryTimer);
                    retryTimer = setTimeout(open, RECONNECT_DELAYS[reconnects++]);
                    return;
                }
                setState('closed');
            };
            timer = setTimeout(function () {
                if (state === 'connecting') unreachable();
            }, CONNECT_TIMEOUT_MS);
        }

        function close() {
            closedByUs = true;
            clearTimeout(retryTimer);
            teardown();
            setState('closed');
        }

        function onVisibility() {
            if (document.hidden) {
                clearTimeout(hiddenTimer);
                hiddenTimer = setTimeout(function () {
                    // Picture-in-picture hides the page but is still watched.
                    if (document.pictureInPictureElement === video) return;
                    if (state === 'live' || state === 'connecting') {
                        closedByUs = true;
                        teardown();
                        setState('paused');
                    }
                }, HIDDEN_CLOSE_MS);
            } else {
                clearTimeout(hiddenTimer);
                if (state === 'paused') open();
            }
        }
        if (opts.pauseWhenHidden !== false) document.addEventListener('visibilitychange', onVisibility);

        function stats() {
            if (!pc) return Promise.resolve(null);
            return pc.getStats().then(function (report) {
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
            reconnect: function () { reconnects = 0; clearTimeout(retryTimer); open(); },
            stats: stats,
            get state() { return state; },
            get info() { return info; },
            destroy: function () {
                document.removeEventListener('visibilitychange', onVisibility);
                clearTimeout(hiddenTimer);
                close();
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
        });

        function flushMove() {
            moveQueued = false;
            var s = stream();
            if (s && pendingMove) s.send({t: 'move', x: pendingMove.x, y: pendingMove.y});
            pendingMove = null;
        }
        target.addEventListener('mousemove', function (event) {
            var point = toDisplay(video, event.clientX, event.clientY);
            if (!point) return;
            pendingMove = point;
            if (!moveQueued) { moveQueued = true; requestAnimationFrame(flushMove); }
        });
        function button(event, down) {
            var point = toDisplay(video, event.clientX, event.clientY);
            var s = stream();
            if (!s) return;
            if (down) target.focus({preventScroll: true});
            if (point) s.send({t: 'move', x: point.x, y: point.y});
            if (point || !down) s.send({t: 'btn', b: event.button + 1, d: down});
            event.preventDefault();
        }
        target.addEventListener('mousedown', function (event) { button(event, true); });
        target.addEventListener('mouseup', function (event) { button(event, false); });
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
