/*
 * Tests for static/env-color-save.js, the environment color save queue.
 *
 * Run: node --test tests/js/   (also part of `uv run scripts.py validate`)
 *
 * Every case drives the controller with a fake save whose responses are
 * resolved by hand, so the order of responses is chosen by the test: delayed
 * responses, rapid clicks, rejected saves. No browser, no network.
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const { createEnvColorSaver } = require('../../static/env-color-save.js');

const HEX = { green: '#4ade80', blue: '#60a5fa', red: '#f87171', cyan: '#22d3ee' };

// A save whose calls are recorded and settled by the test.
function makeSave() {
    const calls = [];
    const save = (name) => new Promise((resolve, reject) => {
        calls.push({ name, resolve: () => resolve({ name, hex: HEX[name] }), reject });
    });
    return { save, calls };
}

function harness() {
    const { save, calls } = makeSave();
    const log = [];
    const saver = createEnvColorSaver({
        confirmed: { name: 'green', hex: HEX.green },
        save,
        onSelect: (n) => log.push('select:' + n),
        onApply: (n, h) => log.push('apply:' + n + ':' + h),
        onSaved: () => log.push('saved'),
        onError: (m) => log.push('error:' + m),
    });
    return { saver, calls, log };
}

const tick = () => new Promise((r) => setImmediate(r));

test('one click saves, applies and acknowledges the confirmed color', async () => {
    const { saver, calls, log } = harness();
    const done = saver.choose('blue');
    assert.deepEqual(log, ['select:blue']);
    assert.equal(calls.length, 1);
    assert.equal(saver.busy(), true);
    calls[0].resolve();
    await done;
    assert.deepEqual(log, ['select:blue', 'select:blue', 'apply:blue:#60a5fa', 'saved']);
    assert.deepEqual(saver.confirmed(), { name: 'blue', hex: HEX.blue });
    assert.equal(saver.busy(), false);
});

test('a click during a save is queued, sent after it, and the latest wins', async () => {
    const { saver, calls, log } = harness();
    const done = saver.choose('blue');
    saver.choose('red');
    saver.choose('cyan');
    // Only one request in flight, whatever the click rate.
    assert.equal(calls.length, 1);
    assert.equal(calls[0].name, 'blue');
    calls[0].resolve();
    await tick();
    // The blue response did not apply: a newer choice was waiting. Only the
    // latest choice was sent, red was skipped.
    assert.equal(calls.length, 2);
    assert.equal(calls[1].name, 'cyan');
    assert.ok(!log.includes('apply:blue:#60a5fa'));
    calls[1].resolve();
    await done;
    assert.equal(log.at(-2), 'apply:cyan:#22d3ee');
    assert.equal(log.at(-1), 'saved');
    assert.deepEqual(saver.confirmed(), { name: 'cyan', hex: HEX.cyan });
});

test('a delayed response never overrides a later confirmed color', async () => {
    const { saver, calls, log } = harness();
    const done = saver.choose('blue');
    saver.choose('red');
    calls[0].resolve();          // blue lands late, red is already queued
    await tick();
    calls[1].resolve();
    await done;
    assert.equal(saver.confirmed().name, 'red');
    assert.equal(log.filter((l) => l.startsWith('apply:')).length, 1);
    assert.equal(log.at(-2), 'apply:red:#f87171');
});

test('a rejected save restores the confirmed selection and shows the failure', async () => {
    const { saver, calls, log } = harness();
    const done = saver.choose('blue');
    calls[0].reject(new Error('Save failed: Unknown environment color'));
    await done;
    assert.deepEqual(log, [
        'select:blue',
        'select:green',
        'error:Save failed: Unknown environment color',
    ]);
    assert.deepEqual(saver.confirmed(), { name: 'green', hex: HEX.green });
    assert.equal(saver.busy(), false);
});

test('a failure with a newer choice waiting is not shown, the newer save decides', async () => {
    const { saver, calls, log } = harness();
    const done = saver.choose('blue');
    saver.choose('red');
    calls[0].reject(new Error('network'));
    await tick();
    assert.ok(!log.some((l) => l.startsWith('error:')));
    calls[1].resolve();
    await done;
    assert.equal(saver.confirmed().name, 'red');
    assert.equal(log.at(-1), 'saved');
});

test('a failure after a confirmed save restores that save, not the page load color', async () => {
    const { saver, calls, log } = harness();
    let done = saver.choose('blue');
    calls[0].resolve();
    await done;
    done = saver.choose('red');
    calls[1].reject(new Error('offline'));
    await done;
    assert.equal(log.at(-2), 'select:blue');
    assert.equal(log.at(-1), 'error:offline');
    assert.equal(saver.confirmed().name, 'blue');
});

test('a rejection without a message still says something', async () => {
    const { saver, calls, log } = harness();
    const done = saver.choose('blue');
    calls[0].reject(undefined);
    await done;
    assert.equal(log.at(-1), 'error:Save failed');
});

test('the server answer is the confirmed color, not the click', async () => {
    // The API normalizes: the confirmed name and hex come from the response.
    const calls = [];
    const log = [];
    const saver = createEnvColorSaver({
        confirmed: { name: 'green', hex: HEX.green },
        save: (name) => { calls.push(name); return Promise.resolve({ name: 'blue', hex: HEX.blue }); },
        onSelect: (n) => log.push('select:' + n),
        onApply: (n, h) => log.push('apply:' + n + ':' + h),
        onSaved: () => log.push('saved'),
        onError: (m) => log.push('error:' + m),
    });
    await saver.choose('Blue');
    assert.deepEqual(calls, ['Blue']);
    assert.equal(log.at(-2), 'apply:blue:#60a5fa');
    assert.equal(saver.confirmed().name, 'blue');
});
