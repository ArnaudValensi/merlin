/*
 * Per-tab tmux session restoration for the web terminal.
 *
 * This file stays pure and has no DOM dependency so Node tests can exercise
 * the same storage and URL logic that the browser runs.
 */

const MerlinSessionIdentity = (() => {
    'use strict';

    const STORAGE_KEY = 'merlin.terminal.session.v1';
    const TMUX_ID = /^\$\d+$/;

    const normalize = (value) => {
        if (!value || typeof value !== 'object') return null;
        const id = value.id;
        const created = Number(value.created);
        if (typeof id !== 'string' || !TMUX_ID.test(id)) return null;
        if (!Number.isSafeInteger(created) || created <= 0) return null;
        return { id, created };
    };

    const storageFor = (provided) => {
        if (provided !== undefined) return provided;
        try {
            return globalThis.sessionStorage;
        } catch (_) {
            return null;
        }
    };

    const read = (provided) => {
        const storage = storageFor(provided);
        if (!storage || typeof storage.getItem !== 'function') return null;
        try {
            const raw = storage.getItem(STORAGE_KEY);
            return raw ? normalize(JSON.parse(raw)) : null;
        } catch (_) {
            return null;
        }
    };

    const write = (value, provided) => {
        const identity = normalize(value);
        const storage = storageFor(provided);
        if (!identity || !storage || typeof storage.setItem !== 'function') return false;
        try {
            storage.setItem(STORAGE_KEY, JSON.stringify(identity));
            return true;
        } catch (_) {
            return false;
        }
    };

    const same = (left, right) =>
        Boolean(left && right && left.id === right.id && left.created === right.created);

    const clearIfMatches = (attempted, provided) => {
        const identity = normalize(attempted);
        const storage = storageFor(provided);
        if (!identity || !storage || typeof storage.removeItem !== 'function') return false;
        if (!same(identity, read(storage))) return false;
        try {
            storage.removeItem(STORAGE_KEY);
            return true;
        } catch (_) {
            return false;
        }
    };

    const clearAfterClose = (attempted, confirmed, closeCode, provided) => {
        if (confirmed || closeCode === 1013 || closeCode === 4401) return false;
        return clearIfMatches(attempted, provided);
    };

    const confirm = (controlFrame, provided) => {
        const identity = normalize(controlFrame);
        if (!identity) return null;
        write(identity, provided);
        return identity;
    };

    const websocketUrl = (location, value) => {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const base = `${proto}//${location.host}/ws/terminal`;
        const identity = normalize(value);
        if (!identity) return base;
        return (
            `${base}?session_id=${encodeURIComponent(identity.id)}` +
            `&session_created=${encodeURIComponent(identity.created)}`
        );
    };

    return {
        STORAGE_KEY,
        read,
        write,
        clearIfMatches,
        clearAfterClose,
        confirm,
        websocketUrl,
    };
})();

if (typeof module !== 'undefined' && module.exports) {
    module.exports = MerlinSessionIdentity;
}
