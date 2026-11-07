/* The board's waiting sync closes the pushed notifications of windows no
   longer waiting, and only those: the workspace crash push ("tmux stopped",
   no window) and the test push stay. Runs the real notifications.js in a VM
   with the browser globals its load touches stubbed. */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load(pushed) {
    const closed = [];
    const list = pushed.map(function (n) {
        return { tag: n.tag, data: n.data, close: function () { closed.push(n.tag); } };
    });
    const registration = {
        getNotifications: function () { return Promise.resolve(list); },
    };
    const noop = function () {};
    const storage = { getItem: function () { return null; }, setItem: noop, removeItem: noop };
    const element = function () {
        return { addEventListener: noop, classList: { add: noop, remove: noop, toggle: noop },
                 style: {}, setAttribute: noop, hidden: false, textContent: '' };
    };
    const document = {
        addEventListener: noop, hidden: false, visibilityState: 'visible',
        getElementById: element, querySelector: element, querySelectorAll: function () { return []; },
        createElement: element, body: element(), documentElement: element(), title: '',
    };
    const window = { addEventListener: noop, location: { origin: 'http://localhost' },
                     localStorage: storage, setTimeout: setTimeout, clearTimeout: clearTimeout };
    const context = {
        window: window, document: document, localStorage: storage,
        navigator: { serviceWorker: { getRegistration: function () { return Promise.resolve(registration); } } },
        Notification: { permission: 'granted', requestPermission: function () { return Promise.resolve('granted'); } },
        fetch: function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } }); },
        setTimeout: setTimeout, clearTimeout: clearTimeout, setInterval: function () { return 0; },
        clearInterval: noop, console: console, Promise: Promise,
    };
    context.window.document = document;
    context.window.navigator = context.navigator;
    vm.createContext(context);
    const source = fs.readFileSync(path.join(__dirname, '../../notifications/static/notifications.js'), 'utf8');
    vm.runInContext(source, context);
    return { api: context.window.MerlinNotifications, closed: closed };
}

test('a handled window notification is closed, the crash and test pushes stay', async () => {
    const { api, closed } = load([
        { tag: 'workspace-restore', data: { url: '/terminal', target: '', sid: '', state: 'crash' } },
        { tag: 'waiting-window', data: { target: 'dev:@3', sid: 'waiting-window', state: 'ask' } },
        { tag: 'handled-window', data: { target: 'dev:@4', sid: 'handled-window', state: 'done' } },
        { tag: 'test', data: { url: '/terminal', target: '', sid: '', state: 'test' } },
    ]);
    assert.ok(api && typeof api.syncWaiting === 'function');
    api.syncWaiting(['waiting-window']);
    await new Promise(function (r) { setTimeout(r, 20); });
    assert.deepEqual(closed, ['handled-window']);
});

test('with nothing waiting every window notification goes, nothing else', async () => {
    const { api, closed } = load([
        { tag: 'workspace-restore', data: { url: '/terminal', target: '', sid: '', state: 'crash' } },
        { tag: 'w1', data: { target: 'a:@1', sid: 'w1', state: 'done' } },
        { tag: 'w2', data: { target: 'a:@2', sid: 'w2', state: 'ask' } },
    ]);
    api.syncWaiting([]);
    await new Promise(function (r) { setTimeout(r, 20); });
    assert.deepEqual(closed.sort(), ['w1', 'w2']);
});
