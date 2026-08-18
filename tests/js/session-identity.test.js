/* Tests for the terminal's per-tab tmux session identity helper. */

const test = require('node:test');
const assert = require('node:assert/strict');

const SessionIdentity = require('../../terminal/templates/session-identity.js');

class MemoryStorage {
    constructor() {
        this.values = new Map();
    }

    getItem(key) {
        return this.values.has(key) ? this.values.get(key) : null;
    }

    setItem(key, value) {
        this.values.set(key, String(value));
    }

    removeItem(key) {
        this.values.delete(key);
    }
}

const identity = (id = '$1', created = 1700000000) => ({ id, created });

test('separate tab stores retain independent identities', () => {
    const tabA = new MemoryStorage();
    const tabB = new MemoryStorage();

    SessionIdentity.write(identity('$1', 10), tabA);
    SessionIdentity.write(identity('$2', 20), tabB);

    assert.deepEqual(SessionIdentity.read(tabA), identity('$1', 10));
    assert.deepEqual(SessionIdentity.read(tabB), identity('$2', 20));
});

test('missing and malformed storage are ignored', () => {
    const storage = new MemoryStorage();
    assert.equal(SessionIdentity.read(storage), null);

    storage.setItem(SessionIdentity.STORAGE_KEY, '{broken');
    assert.equal(SessionIdentity.read(storage), null);

    storage.setItem(SessionIdentity.STORAGE_KEY, JSON.stringify({ id: 'name', created: 1 }));
    assert.equal(SessionIdentity.read(storage), null);
});

test('websocket URL carries an encoded identity before connection', () => {
    const location = { protocol: 'https:', host: 'demo.merlincloud.dev' };

    assert.equal(
        SessionIdentity.websocketUrl(location, identity('$12', 1700000000)),
        'wss://demo.merlincloud.dev/ws/terminal?session_id=%2412&session_created=1700000000',
    );
    assert.equal(
        SessionIdentity.websocketUrl(location, null),
        'wss://demo.merlincloud.dev/ws/terminal',
    );
});

test('a confirmed control frame replaces a stale preference', () => {
    const storage = new MemoryStorage();
    SessionIdentity.write(identity('$1', 10), storage);

    const confirmed = SessionIdentity.confirm(
        { type: 'session', name: 'renamed', id: '$2', created: 20 },
        storage,
    );

    assert.deepEqual(confirmed, identity('$2', 20));
    assert.deepEqual(SessionIdentity.read(storage), identity('$2', 20));
});

test('a malformed control frame is never persisted', () => {
    const storage = new MemoryStorage();
    assert.equal(SessionIdentity.confirm({ type: 'session', id: '$1' }, storage), null);
    assert.equal(SessionIdentity.read(storage), null);
});

test('failed storage access never prevents URL construction or confirmation', () => {
    const blocked = {
        getItem() {
            throw new Error('blocked');
        },
        setItem() {
            throw new Error('blocked');
        },
        removeItem() {
            throw new Error('blocked');
        },
    };

    assert.equal(SessionIdentity.read(blocked), null);
    assert.deepEqual(
        SessionIdentity.confirm({ type: 'session', id: '$1', created: 10 }, blocked),
        identity('$1', 10),
    );
    assert.equal(
        SessionIdentity.websocketUrl({ protocol: 'http:', host: 'localhost:8000' }, null),
        'ws://localhost:8000/ws/terminal',
    );
});

test('failed attempt cleanup cannot erase a newer confirmation', () => {
    const storage = new MemoryStorage();
    const attempted = identity('$1', 10);
    SessionIdentity.write(identity('$2', 20), storage);

    assert.equal(SessionIdentity.clearIfMatches(attempted, storage), false);
    assert.deepEqual(SessionIdentity.read(storage), identity('$2', 20));
    assert.equal(SessionIdentity.clearIfMatches(identity('$2', 20), storage), true);
    assert.equal(SessionIdentity.read(storage), null);
});

test('temporary server failure preserves the attempted identity for retry', () => {
    const storage = new MemoryStorage();
    const attempted = identity('$1', 10);
    SessionIdentity.write(attempted, storage);

    assert.equal(SessionIdentity.clearAfterClose(attempted, false, 1013, storage), false);
    assert.deepEqual(SessionIdentity.read(storage), attempted);
});

test('unconfirmed ordinary close clears the attempted identity', () => {
    const storage = new MemoryStorage();
    const attempted = identity('$1', 10);
    SessionIdentity.write(attempted, storage);

    assert.equal(SessionIdentity.clearAfterClose(attempted, false, 1000, storage), true);
    assert.equal(SessionIdentity.read(storage), null);
});
