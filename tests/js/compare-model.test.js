/* Tests for the Commits page comparison model: selection mode and titles. */

const test = require('node:test');
const assert = require('node:assert/strict');

const M = require('../../commits/static/compare-model.js');

const hashes = ['h1', 'h2', 'h3', 'h4', 'h5']; // newest first

test('picks one endpoint, then a second, then starts over on a third', () => {
    let s = M.emptySelection();
    assert.equal(M.range(s, hashes), null);
    s = M.pick(s, 'h2');
    assert.deepEqual(s, { a: 'h2', b: null });
    assert.equal(M.count(s), 1);
    s = M.pick(s, 'h4');
    assert.deepEqual(s, { a: 'h2', b: 'h4' });
    assert.equal(M.count(s), 2);
    s = M.pick(s, 'h1');
    assert.deepEqual(s, { a: 'h1', b: null });
});

test('tapping a selected endpoint again clears it', () => {
    let s = M.pick(M.pick(M.emptySelection(), 'h2'), 'h4');
    s = M.pick(s, 'h2');
    assert.deepEqual(s, { a: 'h4', b: null });
    s = M.pick(s, 'h4');
    assert.deepEqual(s, { a: null, b: null });
    assert.equal(M.count(s), 0);
});

test('range is inclusive and independent of tap order', () => {
    const forward = M.range(M.pick(M.pick(M.emptySelection(), 'h2'), 'h4'), hashes);
    const backward = M.range(M.pick(M.pick(M.emptySelection(), 'h4'), 'h2'), hashes);
    assert.deepEqual(forward, backward);
    assert.deepEqual(forward, { newest: 'h2', oldest: 'h4', count: 3, hashes: ['h2', 'h3', 'h4'] });
});

test('a single endpoint is a range of one commit', () => {
    const r = M.range(M.pick(M.emptySelection(), 'h3'), hashes);
    assert.deepEqual(r, { newest: 'h3', oldest: 'h3', count: 1, hashes: ['h3'] });
    assert.deepEqual(M.rangeTarget(r), {
        kind: 'compare', base: 'h3^', head: 'h3', mergebase: false, worktree: false,
    });
});

test('an endpoint that left the list gives no range', () => {
    const s = M.pick(M.pick(M.emptySelection(), 'h2'), 'gone');
    assert.equal(M.range(s, hashes), null);
});

test('range target is oldest^..newest', () => {
    const r = M.range(M.pick(M.pick(M.emptySelection(), 'h5'), 'h1'), hashes);
    assert.deepEqual(M.rangeTarget(r), {
        kind: 'compare', base: 'h5^', head: 'h1', mergebase: false, worktree: false,
    });
});

test('summary text pluralizes', () => {
    assert.equal(M.summaryText(1), '1 commit selected');
    assert.equal(M.summaryText(3), '3 commits selected');
    assert.equal(M.commitsText(1), '1 commit');
    assert.equal(M.filesText(2), '2 files');
});

test('default title follows the kind', () => {
    assert.equal(M.title({ kind: 'worktree', worktree: true }), 'Working tree');
    assert.equal(M.title({ kind: 'branch', head: 'feature/x', base: 'main' }), 'feature/x vs main');
    assert.equal(
        M.title({ kind: 'commit', commits: [{ short: 'abc1234', message: 'Fix the thing' }] }),
        'Fix the thing',
    );
    assert.equal(
        M.title({
            kind: 'range',
            commit_count: 3,
            commits: [{ short: 'ccc0000' }, { short: 'bbb0000' }, { short: 'aaa0000' }],
        }),
        'aaa0000..ccc0000 (3 commits)',
    );
    assert.equal(M.title({ kind: 'range', base: 'v1', head: 'v2', commits: [] }), 'v1..v2');
});

test('a range longer than the listed commits takes its start from oldest', () => {
    const listed = [{ short: 'fff0000' }, { short: 'eee0000' }];
    assert.equal(
        M.title({ kind: 'range', commit_count: 350, commits: listed, oldest: { short: '1110000' } }),
        '1110000..fff0000 (350 commits)',
    );
    // Without oldest metadata the last listed commit is the only fallback
    assert.equal(
        M.title({ kind: 'range', commit_count: 2, commits: listed, oldest: null }),
        'eee0000..fff0000 (2 commits)',
    );
});

test('kind labels', () => {
    assert.equal(M.kindLabel('branch'), 'Branch');
    assert.equal(M.kindLabel('worktree'), 'Working tree');
});

test('ref labels shorten full hashes and keep typed refs', () => {
    const h = 'a'.repeat(40);
    assert.equal(M.refLabel(h), 'aaaaaaa');
    assert.equal(M.refLabel(h + '^'), 'aaaaaaa^');
    assert.equal(M.refLabel('feature/x'), 'feature/x');
    assert.equal(M.refLabel('HEAD~3'), 'HEAD~3');
    assert.equal(M.refLabel(''), '');
});

test('viewed progress text', () => {
    assert.equal(M.viewedProgress(0, 3), '0 of 3 viewed');
    assert.equal(M.viewedProgress(3, 3), '3 of 3 viewed');
});

test('a review record maps back to its comparison target', () => {
    assert.deepEqual(
        M.targetOfReview({ kind: 'branch', base: 'main', head: 'feature/x', mergebase: true }),
        { kind: 'compare', base: 'main', head: 'feature/x', mergebase: true, worktree: false },
    );
    assert.deepEqual(
        M.targetOfReview({ kind: 'worktree', base: 'HEAD', head: '', mergebase: false }),
        { kind: 'compare', base: 'HEAD', head: '', mergebase: false, worktree: true },
    );
    assert.deepEqual(
        M.targetOfReview({ kind: 'range', base: 'abc^', head: 'def' }),
        { kind: 'compare', base: 'abc^', head: 'def', mergebase: false, worktree: false },
    );
});
