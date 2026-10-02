/* Full-screen player for one app (phase 3: stream + desktop input). */
(function () {
    'use strict';
    var root = document.getElementById('player');
    var video = document.getElementById('player-video');
    var status = document.getElementById('player-status');
    var id = root.getAttribute('data-id');
    var stream = null;

    var MESSAGES = {
        connecting: 'Connecting…',
        live: '',
        replaced: 'Opened on another device.',
        paused: 'Paused while hidden.',
        closed: 'Disconnected.'
    };

    function onState(state, detail) {
        if (state === 'unreachable') {
            status.textContent = "Can't reach " + (detail.host || 'this machine') +
                ' directly. Join the same Wi-Fi, or use Tailscale.';
        } else if (state === 'exited') {
            status.textContent = detail.reason === 'stopped' ? 'The app was stopped.' :
                'The app exited' + (detail.code != null ? ' (code ' + detail.code + ').' : '.');
        } else if (state === 'error') {
            status.textContent = detail.message || 'Stream error.';
        } else {
            status.textContent = MESSAGES[state] || '';
        }
    }

    stream = MerlinApps.connect({id: id, video: video, onState: onState});
    MerlinApps.bindDesktopInput(root, video, function () { return stream; });
    root.focus({preventScroll: true});
    window.MerlinPlayer = {get stream() { return stream; }};
})();
