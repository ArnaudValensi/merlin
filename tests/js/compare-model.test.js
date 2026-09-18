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

test('singleFlight shares one pending call and then allows a new one', async () => {
    let calls = 0;
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const create = M.singleFlight(async () => { calls += 1; await gate; return 'id-' + calls; });
    const a = create();
    const b = create();
    assert.equal(a, b);
    release();
    assert.equal(await a, 'id-1');
    assert.equal(await b, 'id-1');
    assert.equal(calls, 1);
    assert.equal(await create(), 'id-2');
});

test('singleFlight clears after a failure so the next call retries', async () => {
    let n = 0;
    const create = M.singleFlight(async () => { n += 1; if (n === 1) throw new Error('boom'); return n; });
    await assert.rejects(create(), /boom/);
    assert.equal(await create(), 2);
});

test('serialQueue runs tasks one after another, failures included', async () => {
    const order = [];
    const enqueue = M.serialQueue();
    const first = enqueue(async () => { await new Promise(r => setTimeout(r, 20)); order.push('a'); return 'a'; });
    const second = enqueue(async () => { order.push('b'); throw new Error('b failed'); });
    const third = enqueue(async () => { order.push('c'); return 'c'; });
    assert.equal(await first, 'a');
    await assert.rejects(second, /b failed/);
    assert.equal(await third, 'c');
    assert.deepEqual(order, ['a', 'b', 'c']);
});

test('filterRefs matches branches by name and commits by hash or subject', () => {
    const refs = {
        current: 'feature/x',
        local: ['main', 'feature/x'],
        remote: ['origin/main'],
        commits: [
            { hash: 'a'.repeat(40), short: 'aaaaaaa', message: 'Add the sheet' },
            { hash: 'b'.repeat(40), short: 'bbbbbbb', message: 'Fix main' },
        ],
    };
    let r = M.filterRefs(refs, '');
    assert.deepEqual(r.branches.map(b => b.name), ['main', 'feature/x', 'origin/main']);
    assert.equal(r.branches[1].current, true);
    assert.equal(r.branches[2].remote, true);
    assert.equal(r.commits.length, 2);
    r = M.filterRefs(refs, 'MAIN');
    assert.deepEqual(r.branches.map(b => b.name), ['main', 'origin/main']);
    assert.deepEqual(r.commits.map(c => c.short), ['bbbbbbb']);
    r = M.filterRefs(refs, 'aaaa');
    assert.deepEqual(r.branches, []);
    assert.deepEqual(r.commits.map(c => c.short), ['aaaaaaa']);
    assert.equal(M.refIsListed(refs, 'main'), true);
    assert.equal(M.refIsListed(refs, 'aaaaaaa'), true);
    assert.equal(M.refIsListed(refs, 'HEAD~3'), false);
    assert.equal(M.refIsListed(refs, ''), true);
});

test('mergeBaseNote speaks only when the merge base is not the base', () => {
    const same = { mergebase: true, merge_base: 'x'.repeat(40), base_resolved: 'x'.repeat(40), merge_base_short: 'xxxxxxx' };
    assert.equal(M.mergeBaseNote(same), '');
    const moved = { mergebase: true, merge_base: 'y'.repeat(40), base_resolved: 'x'.repeat(40), merge_base_short: 'yyyyyyy' };
    assert.equal(M.mergeBaseNote(moved), 'from merge base yyyyyyy');
    assert.equal(M.mergeBaseNote({ mergebase: false }), '');
    assert.equal(M.mergeBaseNote(null), '');
});

test('activitySince lists the agent side newest first and skips the user', () => {
    const review = { comments: [
        { id: 'c1', author: 'user', created: '2026-09-18T10:00:00+00:00', path: 'a.txt', line: 2, side: 'new', body: 'Why?',
          status: 'resolved', resolved_at: '2026-09-18T10:20:00+00:00', resolved_by: 'agent',
          replies: [{ id: 'r1', author: 'agent', created: '2026-09-18T10:19:00+00:00', body: 'Because the spec says so, see decision 4 of the requirements file which is long' }] },
        { id: 'c2', author: 'agent', created: '2026-09-18T10:30:00+00:00', path: null, body: 'Overall fine', status: 'open', replies: [] },
        { id: 'c3', author: 'user', created: '2026-09-18T10:40:00+00:00', path: 'b.txt', line: 1, side: 'old', body: 'mine', status: 'open', replies: [] },
        { id: 'c0', author: 'agent', created: '2026-09-18T09:00:00+00:00', path: null, body: 'old', status: 'open', replies: [] },
    ] };
    const items = M.activitySince(review, '2026-09-18T10:10:00+00:00', 2);
    assert.deepEqual(items.map(i => i.kind), ['commits', 'comment', 'resolved', 'reply']);
    assert.equal(items[0].count, 2);
    assert.equal(items[1].commentId, 'c2');
    assert.equal(items[2].commentId, 'c1');
    assert.equal(items[3].path, 'a.txt');
    assert.deepEqual(M.unreadThreads(items), { c2: true, c1: true });
    // No previous visit: everything from the agent counts, nothing from the user
    assert.equal(M.activitySince(review, null, 0).length, 4);
    assert.equal(M.activitySince({ comments: [] }, null, 0).length, 0);
});

test('excerpt collapses whitespace and clips with an ellipsis', () => {
    assert.equal(M.excerpt('  a\n  b  '), 'a b');
    const long = 'x'.repeat(100);
    assert.equal(M.excerpt(long).length, 80);
    assert.ok(M.excerpt(long).endsWith('\u2026'));
    assert.equal(M.excerpt('short', 10), 'short');
});
