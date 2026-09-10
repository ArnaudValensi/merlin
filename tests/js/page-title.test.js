/* Tests for shared machine-first browser title formatting. */

const test = require('node:test');
const assert = require('node:assert/strict');

const PageTitle = require('../../static/page-title.js');

test('formats machine, lowercase app, and context in hierarchy order', () => {
    assert.equal(
        PageTitle.format('worker-1', 'TERM', 'api/main'),
        'worker-1 · term: api/main',
    );
});

test('omits missing segments and reserves Merlin for the empty fallback', () => {
    assert.equal(PageTitle.format('worker-1', 'files'), 'worker-1 · files');
    assert.equal(PageTitle.format('', 'notes', 'Roadmap'), 'notes: Roadmap');
    assert.equal(PageTitle.format('worker-1', '', ''), 'worker-1');
    assert.equal(PageTitle.format('', '', ''), 'Merlin');
});

test('normalizes whitespace and removes title control characters', () => {
    assert.equal(
        PageTitle.format(' worker-1\n', ' FILES ', '  release\t plan\u0007 '),
        'worker-1 · files: release plan',
    );
});

test('extracts compact filesystem context', () => {
    assert.equal(PageTitle.pathContext('/home/user/marketing/'), 'marketing');
    assert.equal(PageTitle.pathContext('/home/user/app.py'), 'app.py');
    assert.equal(PageTitle.pathContext('/'), '/');
    assert.equal(PageTitle.pathContext(''), '');
});

test('composes tmux session and window with graceful partial metadata', () => {
    assert.equal(PageTitle.tmuxContext('api', 'main'), 'api/main');
    assert.equal(PageTitle.tmuxContext('api', ''), 'api');
    assert.equal(PageTitle.tmuxContext('', 'main'), 'main');
});

test('sets the document title from its machine data attribute', () => {
    const doc = {
        documentElement: { dataset: { machineName: 'ovh' } },
        title: '',
    };

    assert.equal(PageTitle.set('FILES', 'marketing', doc), 'ovh · files: marketing');
    assert.equal(doc.title, 'ovh · files: marketing');
});

test('setCount prefixes the title and survives a later set()', () => {
    const doc = {
        documentElement: { dataset: { machineName: 'ovh' } },
        title: '',
    };

    PageTitle.set('term', 'api/main', doc);
    assert.equal(PageTitle.setCount(3, doc), '(3) ovh · term: api/main');
    assert.equal(doc.title, '(3) ovh · term: api/main');
    // A session switch re-titles the page: the count stays.
    assert.equal(PageTitle.set('term', 'api/review', doc), '(3) ovh · term: api/review');
    assert.equal(PageTitle.setCount(0, doc), 'ovh · term: api/review');
    assert.equal(PageTitle.setCount(-2, doc), 'ovh · term: api/review');
    assert.equal(PageTitle.setCount('x', doc), 'ovh · term: api/review');
});

test('withCount formats the prefix only for a positive count', () => {
    assert.equal(PageTitle.withCount('ovh · term', 2), '(2) ovh · term');
    assert.equal(PageTitle.withCount('ovh · term', 0), 'ovh · term');
});
