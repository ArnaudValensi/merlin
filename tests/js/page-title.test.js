/* Tests for the shared browser title formatting: context · app · machine. */

const test = require('node:test');
const assert = require('node:assert/strict');

const PageTitle = require('../../static/page-title.js');

test('formats context, lowercase app, then machine, the specific part first', () => {
    assert.equal(
        PageTitle.format('worker-1', 'TERM', 'api/main'),
        'api/main · term · worker-1',
    );
});

test('omits missing segments and reserves Merlin for the empty fallback', () => {
    assert.equal(PageTitle.format('worker-1', 'files'), 'files · worker-1');
    assert.equal(PageTitle.format('', 'notes', 'Roadmap'), 'Roadmap · notes');
    assert.equal(PageTitle.format('worker-1', '', ''), 'worker-1');
    assert.equal(PageTitle.format('', '', ''), 'Merlin');
});

test('normalizes whitespace and removes title control characters', () => {
    assert.equal(
        PageTitle.format(' worker-1\n', ' FILES ', '  release\t plan\u0007 '),
        'release plan · files · worker-1',
    );
});

test('extracts compact filesystem context', () => {
    assert.equal(PageTitle.pathContext('/home/user/marketing/'), 'marketing');
    assert.equal(PageTitle.pathContext('/home/user/app.py'), 'app.py');
    assert.equal(PageTitle.pathContext('/'), '/');
    assert.equal(PageTitle.pathContext(''), '');
});

test('composes the tmux window then the session, with graceful partial metadata', () => {
    assert.equal(PageTitle.tmuxContext('api', 'main'), 'main · api');
    assert.equal(PageTitle.tmuxContext('api', ''), 'api');
    assert.equal(PageTitle.tmuxContext('', 'main'), 'main');
});

test('sets the document title from its machine data attribute', () => {
    const doc = {
        documentElement: { dataset: { machineName: 'ovh' } },
        title: '',
    };

    assert.equal(PageTitle.set('FILES', 'marketing', doc), 'marketing · files · ovh');
    assert.equal(doc.title, 'marketing · files · ovh');
});

test('setCount prefixes the title and survives a later set()', () => {
    const doc = {
        documentElement: { dataset: { machineName: 'ovh' } },
        title: '',
    };

    PageTitle.set('term', 'main · api', doc);
    assert.equal(PageTitle.setCount(3, doc), '(3) main · api · term · ovh');
    assert.equal(doc.title, '(3) main · api · term · ovh');
    // A session switch re-titles the page: the count stays.
    assert.equal(PageTitle.set('term', 'review · api', doc), '(3) review · api · term · ovh');
    assert.equal(PageTitle.setCount(0, doc), 'review · api · term · ovh');
    assert.equal(PageTitle.setCount(-2, doc), 'review · api · term · ovh');
    assert.equal(PageTitle.setCount('x', doc), 'review · api · term · ovh');
});

test('withCount formats the prefix only for a positive count', () => {
    assert.equal(PageTitle.withCount('term · ovh', 2), '(2) term · ovh');
    assert.equal(PageTitle.withCount('term · ovh', 0), 'term · ovh');
});
