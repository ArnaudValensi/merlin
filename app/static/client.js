/* App stream client: WebRTC, peer to peer when a path exists (LAN, UPnP,
 * STUN) else through Merlin Cloud's relay (TURN), signaling over Merlin's
 * WebSocket. Shared by the full-screen player, the terminal panel and the
 * mobile mini-player. ?ice=relay on the page forces the relay (a test switch).
 *
 *   const stream = MerlinApps.connect({id, video, onState, onWelcome, ...});
 *   stream.send({t: 'key', k: 'Right', d: true});
 *   stream.close();
 *
 * States: connecting, live, unreachable (ICE failed, or nothing after 30 s:
 * no path between the two), replaced (opened on another device), exited (the
 * app ended or was stopped), paused (hidden for a while), error, closed.
 * "connecting" carries {slow: true} after 4 s without a connection, and
 * {reconnecting: true} while a dropped connection is being restored.
 *
 * Sound: the app's audio track plays in the same <video>. Browsers only play
 * sound after a user gesture, so each surface asks for it through
 * MerlinApps.sound(video, surface, gestureTarget), which keeps the surface's
 * remembered choice and applies it on the next touch, click or key.
 */
(function () {
    'use strict';

    // From the offer, ICE's own verdict ("failed") ends a hopeless attempt;
    // this only bounds one that never decides. On a LAN connections take well
    // under a second; over the internet (a phone on 4G) several.
    var CONNECT_TIMEOUT_MS = 30000;
    var SLOW_CONNECT_MS = 4000;      // then the status says we are still trying
    var DROP_GRACE_MS = 5000;        // a live connection "disconnected" this long has dropped
    // ICE "failed" before the first connection: candidates still on their
    // way (STUN's public address, the router's mapping) may revive it.
    var FAILED_GRACE_MS = 6000;
    var DROP_RETRY_DELAYS = [1000, 2000, 4000];  // new sessions after a drop
    var NET_REPORT_MS = 2000;        // the round trip, sent to the streamer
    var SETUP_TIMEOUT_MS = 60000;    // from the socket: the server must offer by then
    var HIDDEN_CLOSE_MS = 30000;

    // ---- the link: route and quality ----

    function ipv4(address) {
        var m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(address);
        return m ? [+m[1], +m[2], +m[3], +m[4]] : null;
    }
    function ipv6Head(address) {
        // The first two 16-bit groups, enough for the ranges below.
        if (address.indexOf(':') < 0) return null;
        var parts = address.split('%')[0].split('::');
        var head = parts[0] ? parts[0].split(':') : [];
        var groups = head.map(function (h) { return parseInt(h || '0', 16); });
        while (groups.length < 3) groups.push(0);
        if (parts.length > 1 && !parts[0]) groups = [0, 0, 0];  // "::1"
        return groups;
    }

    /** The route to the machine, from its address in the selected ICE pair
     * (the same rule as app/linkstats.py classify); a relay is a path. */
    function routeOf(address) {
        address = String(address || '');
        var v4 = ipv4(address);
        if (v4) {
            if (v4[0] === 10 || v4[0] === 127 || (v4[0] === 172 && v4[1] >= 16 && v4[1] <= 31) ||
                (v4[0] === 192 && v4[1] === 168) || (v4[0] === 169 && v4[1] === 254) ||
                (v4[0] === 100 && v4[1] >= 64 && v4[1] <= 127)) return 'LAN';
            return 'Internet · IPv4';
        }
        var v6 = ipv6Head(address);
        if (v6) {
            if ((v6[0] & 0xfe00) === 0xfc00 || (v6[0] & 0xffc0) === 0xfe80) return 'LAN';
            if (address === '::1') return 'LAN';
            return 'Internet · IPv6';
        }
        return '?';
    }

    /** Which mechanism carries the stream, from the browser's side of the
     * pair (the streamer's ``path`` message wins: it also knows UPnP). */
    function pathOf(localType, remoteType) {
        if (!localType && !remoteType) return '';
        if (localType === 'relay' || remoteType === 'relay') return 'TURN';
        if (localType === 'srflx' || remoteType === 'srflx') return 'STUN';
        return 'Direct';
    }

    /** The quality gauge, 1 to 4 bars: where the rate controller stands. */
    function levelOf(kbps, max) {
        if (!kbps || !max) return 4;
        var share = kbps / max;
        return share >= 0.85 ? 4 : share >= 0.60 ? 3 : share >= 0.35 ? 2 : 1;
    }

    function mbit(kbps) { return (kbps / 1000).toFixed(1); }

    /** The chip's words: route, path and round trip (a plain LAN connection
     * says only LAN). With detail, more lines: the numbers, the selected
     * pair, and what each way of reaching the machine gave. */
    function chipText(s, detail) {
        var route = s.route || 'LAN';
        var path = s.path && !(route === 'LAN' && s.path === 'Direct') ? ' · ' + s.path : '';
        var rtt = s.rttMs == null ? '' : ' · ' + (s.rttMs < 1 ? '<1' : s.rttMs) + ' ms';
        var text = route + path + rtt;
        if (!detail) return text;
        var numbers = [];
        if (s.kbps != null) {
            numbers.push(mbit(s.kbps) + ' Mbit/s' +
                (s.rateKbps && s.rateMax ? ' (target ' + mbit(s.rateKbps) + '/' + mbit(s.rateMax) + ')' : ''));
        } else if (s.rateKbps && s.rateMax) {
            numbers.push('target ' + mbit(s.rateKbps) + '/' + mbit(s.rateMax) + ' Mbit/s');
        }
        if (s.fps != null) numbers.push(s.fps + ' fps');
        if (s.lossPct != null) numbers.push('loss ' + s.lossPct.toFixed(1) + ' %');
        if (s.codec) numbers.push(s.codec + (s.encoder ? ' (' + s.encoder + ')' : ''));
        var lines = [text];
        if (numbers.length) lines.push(numbers.join(' · '));
        if (s.local || s.remote) lines.push((s.local || '?') + ' ↔ ' + (s.remote || '?'));
        if (s.reach) {
            lines.push('STUN ' + (s.reach.stun || '?') + ' · UPnP ' + (s.reach.upnp || '?') +
                ' · TURN ' + (s.reach.turn || '?'));
        }
        return lines.join('\n');
    }

    /** Fill a chip: its text, then the gauge (built once, kept). */
    function renderChip(el, s, detail) {
        var text = el.querySelector('.stream-chip-text');
        var gauge = el.querySelector('.stream-gauge');
        if (!text) {
            el.textContent = '';
            text = document.createElement('span');
            text.className = 'stream-chip-text';
            gauge = document.createElement('span');
            gauge.className = 'stream-gauge';
            gauge.setAttribute('aria-hidden', 'true');
            for (var i = 0; i < 4; i++) gauge.appendChild(document.createElement('i'));
            el.appendChild(gauge);  // first, like a phone's bars: a long text gets cut, not it
            el.appendChild(text);
        }
        text.textContent = chipText(s, detail);
        // Several lines with the detail (the chip's CSS keeps them).
        if (detail) el.setAttribute('data-detail', ''); else el.removeAttribute('data-detail');
        var level = s ? levelOf(s.rateKbps, s.rateMax) : 4;
        gauge.setAttribute('data-level', String(level));
        gauge.hidden = !s;
        el.setAttribute('aria-label', 'Connection: ' + chipText(s, true).replace(/\n/g, ', ') +
            ', quality ' + level + ' of 4');
    }

    /** A chip showing only words (a state, the agent's input). */
    function chipWords(el, words) {
        el.removeAttribute('data-detail');
        el.textContent = words;
        el.setAttribute('aria-label', 'Connection: ' + words);
    }

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

    /** play(), falling back to muted playback when the browser refuses sound
     * without a gesture: the picture must never wait for one. */
    function play(video) {
        var playing = video.play();
        if (!playing || !playing.catch) return;
        playing.catch(function (err) {
            if (err && err.name === 'NotAllowedError' && !video.muted) {
                video.muted = true;
                var again = video.play();
                if (again && again.catch) again.catch(function () {});
            }
        });
    }

    /** Ask for stereo Opus in our answer: without stereo=1 the browser
     * decodes the app's sound as mono. */
    function preferStereo(sdp) {
        var rtpmap = /a=rtpmap:(\d+) opus\/48000\/2/i.exec(sdp);
        if (!rtpmap) return sdp;
        var fmtp = new RegExp('a=fmtp:' + rtpmap[1] + ' ([^\\r\\n]*)');
        if (fmtp.test(sdp)) {
            return sdp.replace(fmtp, function (line, params) {
                return /(^|;)\s*stereo=/.test(params) ? line : line + ';stereo=1';
            });
        }
        return sdp.replace(rtpmap[0], rtpmap[0] + '\r\na=fmtp:' + rtpmap[1] + ' stereo=1');
    }

    // Where sound is on until the user says otherwise: where you play (the
    // full player) and the docked desktop panel; not the mobile mini-player.
    var SOUND_DEFAULTS = {player: true, panel: true, mini: false};

    /** Sound on one surface: its remembered choice, applied on a gesture.
     * Elements marked data-sound (the toggles) are left to their own click. */
    function sound(video, surface, gestureTarget) {
        var storeKey = 'app-sound-' + surface;
        var wanted = !!SOUND_DEFAULTS[surface];
        try {
            var stored = localStorage.getItem(storeKey);
            if (stored === '1' || stored === '0') wanted = stored === '1';
        } catch (e) {}
        var target = gestureTarget || window;
        var GESTURES = ['pointerup', 'touchend', 'keydown'];

        function apply() {
            var mute = !wanted;
            if (video.muted === mute) return;
            video.muted = mute;
            if (!mute && video.srcObject) play(video);
        }
        function onGesture(event) {
            var el = event.target;
            if (el && el.closest && el.closest('[data-sound]')) return;
            if (wanted && video.muted) apply();
        }
        GESTURES.forEach(function (type) { target.addEventListener(type, onGesture, true); });

        return {
            get on() { return !video.muted; },
            get wanted() { return wanted; },
            /** Call from a gesture: the toggle's click, the click that opened. */
            set: function (on) {
                wanted = !!on;
                try { localStorage.setItem(storeKey, wanted ? '1' : '0'); } catch (e) {}
                apply();
            },
            apply: apply,
            destroy: function () {
                GESTURES.forEach(function (type) { target.removeEventListener(type, onGesture, true); });
            }
        };
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

    /** ?ice=relay on the page: through the relay only (to test TURN). */
    function icePolicy() {
        try {
            return new URLSearchParams(location.search).get('ice') === 'relay' ? 'relay' : '';
        } catch (e) { return ''; }
    }

    /** The peer, with the servers the server sent (STUN, TURN); a browser
     * that refuses them still gets a peer: host candidates, the LAN. */
    function makePeer(servers, policy) {
        var config = {iceServers: Array.isArray(servers) ? servers : []};
        if (policy === 'relay') config.iceTransportPolicy = 'relay';
        try {
            return new RTCPeerConnection(config);
        } catch (e) {
            return new RTCPeerConnection({iceServers: []});
        }
    }

    function connect(opts) {
        var video = opts.video;
        var ws = null, pc = null, channel = null;
        var state = 'closed';
        var timer = null, hiddenTimer = null, retryTimer = null;
        var lastBytes = null;
        var lastLoss = null;
        var slowTimer = null, dropTimer = null, netTimer = null, failTimer = null;
        var drops = 0;   // dropped live connections being restored in a row
        var info = {host: '', app: null, encoder: '', codec: '', audio: false, rate: null, route: '',
                    iceServers: [], policy: 'all', path: null, reach: null};
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
            clearTimeout(slowTimer);
            clearTimeout(dropTimer);
            clearTimeout(failTimer);
            clearInterval(netTimer);
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
            // A new offer replaces the connection (the streamer restarted
            // its pipeline, without sound): the previous peer is closed, and
            // everything it still had in flight is ignored (mine()).
            if (pc) { try { pc.close(); } catch (e) {} }
            var peer = makePeer(info.iceServers, info.policy);
            pc = peer;
            function mine() { return current(g) && pc === peer; }
            // "Not on the same network" is about ICE only: the clock starts at
            // the offer. Before it, the server may be waiting on another app's
            // start or stop, which is not a network problem.
            clearTimeout(timer);
            timer = setTimeout(function () {
                if (mine() && state === 'connecting') lost();
            }, CONNECT_TIMEOUT_MS);
            clearTimeout(slowTimer);
            slowTimer = setTimeout(function () {
                if (mine() && state === 'connecting' && !drops) setState('connecting', {slow: true});
            }, SLOW_CONNECT_MS);
            var wasLive = false;
            function sendOn(message) {
                if (mine() && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
            }
            // Picture and sound in one element: one stream per connection
            // collects both tracks, whatever streams the offer groups them in.
            var media = new MediaStream();
            peer.ontrack = function (event) {
                if (!mine()) return;
                media.addTrack(event.track);
                if (video.srcObject !== media) video.srcObject = media;
                play(video);
            };
            peer.ondatachannel = function (event) { if (mine()) channel = event.channel; };
            peer.onicecandidate = function (event) {
                if (event.candidate) {
                    sendOn({type: 'ice', candidate: event.candidate.candidate,
                            sdpMLineIndex: event.candidate.sdpMLineIndex});
                }
            };
            peer.onconnectionstatechange = function () {
                if (!mine()) return;
                var now = peer.connectionState;
                if (now === 'connected') {
                    clearTimeout(timer);
                    clearTimeout(slowTimer);
                    clearTimeout(dropTimer);
                    clearTimeout(failTimer);
                    reconnects = 0;
                    drops = 0;
                    wasLive = true;
                    setState('live');
                    clearInterval(netTimer);
                    netTimer = setInterval(function () { if (mine()) reportNet(peer); }, NET_REPORT_MS);
                } else if (now === 'disconnected' && wasLive) {
                    // Often back within a second (a Wi-Fi blip); a phone that
                    // changed networks is not: then a new session.
                    clearTimeout(dropTimer);
                    dropTimer = setTimeout(function () {
                        if (mine() && peer.connectionState !== 'connected') dropped();
                    }, DROP_GRACE_MS);
                } else if (now === 'failed') {
                    if (wasLive) { dropped(); return; }
                    clearTimeout(failTimer);
                    failTimer = setTimeout(function () {
                        if (mine() && peer.connectionState !== 'connected') lost();
                    }, FAILED_GRACE_MS);
                }
            };
            peer.setRemoteDescription({type: 'offer', sdp: sdp})
                .then(function () { return peer.createAnswer(); })
                .then(function (answer) {
                    if (!mine()) return;
                    var local = {type: 'answer', sdp: preferStereo(answer.sdp)};
                    return peer.setLocalDescription(local).then(function () {
                        sendOn({type: 'answer', sdp: local.sdp});
                    });
                })
                .catch(function (err) {
                    if (mine()) fail('Could not start the stream: ' + err);
                });
        }

        function unreachable() {
            teardown();
            drops = 0;
            setState('unreachable', {host: info.host});
        }

        /** The round trip as the browser measures it (its ICE checks), for
         * the streamer's rate control: it has no measure of its own. */
        function reportNet(peer) {
            // The answer comes later: it belongs to this peer and this channel
            // only, never to a session that replaced them meanwhile.
            var g = gen, ch = channel;
            if (!ch || ch.readyState !== 'open') return;
            peer.getStats().then(function (report) {
                if (!current(g) || pc !== peer || channel !== ch) return;
                var pair = null, byId = {};
                report.forEach(function (s) {
                    byId[s.id] = s;
                    if (s.type === 'transport' && s.selectedCandidatePairId) pair = s.selectedCandidatePairId;
                });
                var found = pair ? byId[pair] : null;
                if (found && found.currentRoundTripTime != null && ch.readyState === 'open') {
                    ch.send(JSON.stringify({t: 'net', rtt: found.currentRoundTripTime * 1000}));
                }
            }).catch(function () {});
        }

        /** A session that never connected: unreachable, unless it was one of
         * the new sessions after a drop, which gets the next try. */
        function lost() {
            if (drops) dropped(); else unreachable();
        }

        /** A live connection was lost: a few new sessions, then unreachable. */
        function dropped() {
            if (drops >= DROP_RETRY_DELAYS.length) { unreachable(); return; }
            teardown();
            setState('connecting', {reconnecting: true});
            schedule(open, DROP_RETRY_DELAYS[drops++]);
        }

        function fail(message) {
            teardown();
            if (destroyed) return;
            // While restoring a dropped connection, every failure (no offer
            // in time, a streamer error) spends one of its tries: the
            // dead-streamer retry below would otherwise refresh forever.
            if (drops) { dropped(); return; }
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
                    var hello = {type: 'hello', codecs: browserCodecs()};
                    if (icePolicy()) hello.ice = icePolicy();
                    socket.send(JSON.stringify(hello));
                    break;
                case 'ready':
                    info.encoder = msg.encoder || '';
                    info.codec = msg.codec || '';
                    info.audio = !!msg.audio;
                    if (video && video.parentElement) {
                        video.parentElement.setAttribute('data-encoder', info.encoder);
                        video.parentElement.setAttribute('data-codec', info.codec);
                        video.parentElement.setAttribute('data-audio', info.audio ? '1' : '0');
                    }
                    if (opts.onReady) opts.onReady(info);
                    break;
                case 'rate':
                    info.rate = {kbps: msg.kbps, max: msg.max};
                    break;
                case 'route':
                    info.route = String(msg.route || '');
                    break;
                case 'servers':
                    info.iceServers = Array.isArray(msg.iceServers) ? msg.iceServers : [];
                    info.policy = msg.policy === 'relay' ? 'relay' : 'all';
                    break;
                case 'path':
                    info.path = {path: String(msg.path || ''), local: String(msg.local || ''),
                                 remote: String(msg.remote || '')};
                    break;
                case 'reach':
                    info.reach = {stun: String(msg.stun || ''), upnp: String(msg.upnp || ''),
                                  turn: String(msg.turn || '')};
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
                case 'restarted':
                    // The app was relaunched: stream the new launch.
                    open();
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
            lastLoss = null;
            info.rate = null;
            info.route = '';
            info.iceServers = [];
            info.policy = 'all';
            info.path = null;
            info.reach = null;
            // Always with a detail: from "reconnecting" to a plain start the
            // state stays "connecting", and the status must still change.
            setState('connecting', drops ? {reconnecting: true} : {});
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
                if (current(g) && state === 'connecting') fail('The stream did not start.');
            }, SETUP_TIMEOUT_MS);
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
            drops = 0;  // a resume is a fresh start, not a reconnection
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
                var out = {rttMs: null, fps: null, kbps: null, codec: info.codec, encoder: info.encoder,
                           route: '', lossPct: null, path: '', local: '', remote: '', reach: info.reach,
                           rateKbps: info.rate ? info.rate.kbps : null,
                           rateMax: info.rate ? info.rate.max : null};
                var byId = {};
                var codecs = {};
                var selected = null;
                report.forEach(function (s) {
                    byId[s.id] = s;
                    if (s.type === 'codec') codecs[s.id] = s;
                    if (s.type === 'transport' && s.selectedCandidatePairId) selected = s.selectedCandidatePairId;
                });
                var pair = selected ? byId[selected] : null;
                if (!pair) {
                    report.forEach(function (s) {
                        if (s.type === 'candidate-pair' && s.nominated && s.state === 'succeeded') pair = s;
                    });
                }
                if (pair) {
                    if (pair.currentRoundTripTime != null) out.rttMs = Math.round(pair.currentRoundTripTime * 1000);
                    var remote = byId[pair.remoteCandidateId] || {};
                    var local = byId[pair.localCandidateId] || {};
                    // The streamer's reading wins: it sees both ends (ours is
                    // hidden from us, so a public IPv6 at home reads as internet).
                    out.route = info.route ||
                        routeOf(remote.address || remote.ip);
                    out.path = info.path ? info.path.path : pathOf(local.candidateType, remote.candidateType);
                    if (info.path) { out.local = info.path.local; out.remote = info.path.remote; }
                }
                report.forEach(function (s) {
                    if (s.type === 'inbound-rtp' && s.kind === 'video') {
                        var lost = s.packetsLost || 0, got = s.packetsReceived || 0;
                        if (lastLoss && got + lost > lastLoss.got + lastLoss.lost) {
                            var dLost = Math.max(0, lost - lastLoss.lost);
                            out.lossPct = 100 * dLost / (got - lastLoss.got + dLost);
                        }
                        lastLoss = {lost: lost, got: got};
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
            // The user's Retry: a fresh start, with every automatic budget back.
            reconnect: function () { reconnects = 0; drops = 0; open(); },
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

        // Keys go to the app only while it is live, and never those meant for
        // a control over the video (Retry, Logs, Leave): Tab, Enter and Space
        // keep working on the page around a stream that is not playing.
        function forApp(event) {
            if (onControl(event)) return false;
            var s = stream();
            return !!s && s.state === 'live';
        }
        target.addEventListener('keydown', function (event) {
            if (event.metaKey && event.key !== 'Meta') return; // leave OS shortcuts alone
            if (!forApp(event)) return;
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
            if (onControl(event)) return;
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
            if (Object.keys(buttonsHeld).length) syncButtons(event);  // chords
            var point = toDisplay(video, event.clientX, event.clientY);
            if (!point) return;
            pendingMove = point;
            if (!moveQueued) { moveQueued = true; requestAnimationFrame(flushMove); }
        });
        // Buttons: pointer capture keeps the release coming to us when the drag
        // ends outside the video (over the terminal); whatever is still held
        // when capture or focus is lost is released, never left pressed.
        // Pointer events report a second button pressed or released during a
        // drag (a chord) as a pointermove, so held buttons are reconciled with
        // the `buttons` bitmask on every event rather than per down/up.
        var BUTTON_BITS = [[1, 1], [2, 3], [4, 2], [8, 8], [16, 9]];  // [bit, X button]
        var buttonsHeld = {};
        function releaseButtons() {
            var s = stream();
            Object.keys(buttonsHeld).forEach(function (b) {
                if (s) s.send({t: 'btn', b: +b, d: false});
            });
            buttonsHeld = {};
        }
        function syncButtons(event) {
            var s = stream();
            if (!s) return;
            var point = toDisplay(video, event.clientX, event.clientY);
            BUTTON_BITS.forEach(function (pair) {
                var down = (event.buttons & pair[0]) !== 0;
                var b = pair[1];
                if (down && !buttonsHeld[b]) {
                    if (point) s.send({t: 'move', x: point.x, y: point.y});
                    s.send({t: 'btn', b: b, d: true});
                    buttonsHeld[b] = true;
                } else if (!down && buttonsHeld[b]) {
                    if (point) s.send({t: 'move', x: point.x, y: point.y});
                    s.send({t: 'btn', b: b, d: false});
                    delete buttonsHeld[b];
                }
            });
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
            target.focus({preventScroll: true});
            event.preventDefault();
            if (!stream() || !point) return;
            try { target.setPointerCapture(event.pointerId); } catch (e) {}
            syncButtons(event);
        });
        target.addEventListener('pointerup', function (event) {
            if (event.pointerType === 'touch' || !Object.keys(buttonsHeld).length) return;
            syncButtons(event);
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
        routeOf: routeOf,
        pathOf: pathOf,
        levelOf: levelOf,
        chipText: chipText,
        renderChip: renderChip,
        chipWords: chipWords,
        sound: sound,
        preferStereo: preferStereo,
        toDisplay: toDisplay,
        keysymFor: keysymFor,
        bindDesktopInput: bindDesktopInput
    };
})();
