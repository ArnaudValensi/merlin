/* Tests for the per-browser choice between the classic and the native
   terminal (terminal/templates/terminal-choice.js): where each page goes. */

const test = require('node:test');
const assert = require('node:assert/strict');

const Choice = require('../../terminal/templates/terminal-choice.js');

const plan = (page, search, stored = null, fallback = '') => Choice.plan(page, search, stored, fallback);

test('the classic page stays by default', () => {
    assert.deepEqual(plan('classic', ''), {
        store: null, clearFallback: false, go: null, query: '', dropParam: false,
    });
    assert.equal(plan('classic', '', 'classic').go, null);
    assert.equal(plan('classic', '', 'something else').go, null);
});

test('a stored native choice sends /terminal to the native page with its query', () => {
    const p = plan('classic', '?target=dev:@3', 'native');
    assert.equal(p.go, '/terminal/native');
    assert.equal(p.query, '?target=dev:@3');
    assert.equal(p.store, null);
});

test('?terminal=native is stored, dropped from the query, and clears a fallback', () => {
    const p = plan('classic', '?terminal=native&target=dev:@3', null, 'tmux too old');
    assert.equal(p.store, 'native');
    assert.equal(p.go, '/terminal/native');
    assert.equal(p.query, '?target=dev:@3');
    assert.equal(p.clearFallback, true);
});

test('?terminal=classic on the classic page is stored and only dropped', () => {
    const p = plan('classic', '?terminal=classic&x=1', 'native');
    assert.equal(p.store, 'classic');
    assert.equal(p.go, null);
    assert.equal(p.dropParam, true);
    assert.equal(p.query, '?x=1');
});

test('a tab the native page sent back stays on the classic page', () => {
    assert.equal(plan('classic', '', 'native', 'tmux 3.4 is older than 3.8').go, null);
});

test('the native page stays, whatever is stored, unless asked for the classic one', () => {
    assert.equal(plan('native', '', 'classic').go, null);
    assert.equal(plan('native', '', null).go, null);
    const p = plan('native', '?terminal=classic&target=dev:@1', 'native');
    assert.equal(p.go, '/terminal');
    assert.equal(p.store, 'classic');
    assert.equal(p.query, '?target=dev:@1');
});

test('an unknown value of the parameter changes nothing but is dropped', () => {
    const p = plan('classic', '?terminal=fancy', null);
    assert.equal(p.store, null);
    assert.equal(p.go, null);
    assert.equal(p.dropParam, true);
});

test('the fall back notice shows on the classic page only, right after it or while native is saved', () => {
    const why = 'tmux 3.4 is older than 3.8';
    // A direct visit to /terminal/native with Classic (or nothing) saved.
    assert.equal(Choice.showsFallback('classic', null, why, '1'), true);
    assert.equal(Choice.showsFallback('classic', 'classic', why, '1'), true);
    // Later loads of the classic page: only while native is the saved choice.
    assert.equal(Choice.showsFallback('classic', 'classic', why, null), false);
    assert.equal(Choice.showsFallback('classic', null, why, null), false);
    assert.equal(Choice.showsFallback('classic', 'native', why, null), true);
    // Never on the native page, never without a fall back.
    assert.equal(Choice.showsFallback('native', 'native', why, '1'), false);
    assert.equal(Choice.showsFallback('classic', 'native', '', '1'), false);
});
