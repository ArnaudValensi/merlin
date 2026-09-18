/* Pure model for the Commits page comparisons: the list's selection mode and
   the words the page uses for a comparison. No DOM, so node can test it
   (tests/js/compare-model.test.js). Loaded by commits.js. */

var CompareModel = (function() {
    'use strict';

    // A selection is two endpoints, `a` and `b`, each a commit hash or null.
    // Order of taps is irrelevant: the range is computed from list order.
    function emptySelection() {
        return { a: null, b: null };
    }

    // Tap a row. A selected endpoint tapped again clears it. With both
    // endpoints set, a third tap starts over from that row.
    function pick(state, hash) {
        if (state.a === hash) return { a: state.b, b: null };
        if (state.b === hash) return { a: state.a, b: null };
        if (state.a === null) return { a: hash, b: null };
        if (state.b === null) return { a: state.a, b: hash };
        return { a: hash, b: null };
    }

    function count(state) {
        return (state.a ? 1 : 0) + (state.b ? 1 : 0);
    }

    // The range covered by the selection over `hashes` (the list, newest
    // first). Both endpoints inclusive. Null until at least one endpoint is
    // picked or when an endpoint is no longer in the list.
    function range(state, hashes) {
        if (!state.a) return null;
        var i = hashes.indexOf(state.a);
        if (i < 0) return null;
        var j = i;
        if (state.b) {
            j = hashes.indexOf(state.b);
            if (j < 0) return null;
        }
        var lo = Math.min(i, j);
        var hi = Math.max(i, j);
        return {
            newest: hashes[lo],
            oldest: hashes[hi],
            count: hi - lo + 1,
            hashes: hashes.slice(lo, hi + 1),
        };
    }

    // The comparison a range opens: oldest^..newest. The server maps a root
    // commit's missing parent to the empty tree.
    function rangeTarget(r) {
        return { kind: 'compare', base: r.oldest + '^', head: r.newest, mergebase: false, worktree: false };
    }

    function summaryText(n) {
        return n + ' commit' + (n === 1 ? '' : 's') + ' selected';
    }

    // The default title of a comparison (and of the review saved from it).
    function title(detail) {
        if (detail.worktree || detail.kind === 'worktree') return 'Working tree';
        if (detail.kind === 'branch') return detail.head + ' vs ' + detail.base;
        var commits = detail.commits || [];
        if (detail.kind === 'commit' && commits.length === 1) return commits[0].message;
        if (commits.length > 0) {
            var n = detail.commit_count || commits.length;
            // The listed commits are capped: the range's start is the server's
            // `oldest`, never the last listed one.
            var oldest = (detail.oldest || commits[commits.length - 1]).short;
            var newest = commits[0].short;
            return oldest + '..' + newest + ' (' + n + ' commit' + (n === 1 ? '' : 's') + ')';
        }
        return detail.base + '..' + detail.head;
    }

    // A ref as the header shows it: a full hash (with an optional ^ from
    // the list's Select mode) is shortened, anything typed stays as typed.
    function refLabel(ref) {
        var m = /^([0-9a-f]{40})(\^?)$/.exec(ref || '');
        return m ? m[1].slice(0, 7) + m[2] : (ref || '');
    }

    // One flight at a time: while a call of `fn` is pending, every further
    // call gets the same promise. Used for the implicit review creation, so
    // two quick ticks create one review and both wait for its id.
    function singleFlight(fn) {
        var pending = null;
        return function() {
            if (pending) return pending;
            pending = Promise.resolve().then(fn).finally(function() { pending = null; });
            return pending;
        };
    }

    // A serial queue: each task starts after the previous one settled, so
    // two mutations of one record never race each other's response.
    function serialQueue() {
        var tail = Promise.resolve();
        return function(task) {
            var run = tail.then(task, task);
            tail = run.catch(function() {});
            return run;
        };
    }

    // "k of n viewed" for the files panel toggle.
    function viewedProgress(k, n) {
        return k + ' of ' + n + ' viewed';
    }

    function kindLabel(kind) {
        return { commit: 'Commit', range: 'Range', branch: 'Branch', worktree: 'Working tree' }[kind] || kind;
    }

    function commitsText(n) {
        return n + ' commit' + (n === 1 ? '' : 's');
    }

    function filesText(n) {
        return n + ' file' + (n === 1 ? '' : 's');
    }

    // The sheet's pickers: branches (local first, then remote) and the
    // recent commits, filtered by `q` on the branch name, the short hash or
    // the subject. Case-insensitive, empty `q` keeps everything.
    function filterRefs(refs, q) {
        q = (q || '').trim().toLowerCase();
        var hit = function(text) { return !q || (text || '').toLowerCase().indexOf(q) >= 0; };
        var branches = [];
        (refs.local || []).forEach(function(name) {
            if (hit(name)) branches.push({ name: name, remote: false, current: name === refs.current });
        });
        (refs.remote || []).forEach(function(name) {
            if (hit(name)) branches.push({ name: name, remote: true, current: false });
        });
        var commits = (refs.commits || []).filter(function(c) {
            return hit(c.short) || hit(c.hash) || hit(c.message);
        });
        return { branches: branches, commits: commits };
    }

    // Whether the typed text names a listed branch or commit exactly: then
    // the "use as typed" row is redundant.
    function refIsListed(refs, text) {
        text = (text || '').trim();
        if (!text) return true;
        if ((refs.local || []).indexOf(text) >= 0 || (refs.remote || []).indexOf(text) >= 0) return true;
        return (refs.commits || []).some(function(c) { return c.hash === text || c.short === text; });
    }

    // The merge base is worth a word only when it is not the base itself:
    // when the base branch moved on since the fork, the diff starts at the
    // fork, not at the base's tip.
    function mergeBaseNote(meta) {
        if (!meta || !meta.mergebase || !meta.merge_base) return '';
        if (meta.merge_base === meta.base_resolved) return '';
        return 'from merge base ' + (meta.merge_base_short || meta.merge_base.slice(0, 7));
    }

    // What happened on a review since the user's previous visit, newest
    // first: commits that landed on the head, and the agent's comments,
    // replies and resolutions (the user's own actions are theirs to know).
    // `seenBefore` is the previous visit's ISO stamp, so items the agent
    // adds while the page is open (they arrive by the poll) also qualify.
    function activitySince(review, seenBefore, newCommits) {
        var items = [];
        var since = seenBefore || '';
        var after = function(ts) { return !!ts && (!since || ts > since); };
        if (newCommits > 0) items.push({ kind: 'commits', count: newCommits, ts: '\uffff' });
        ((review && review.comments) || []).forEach(function(c) {
            var where = { commentId: c.id, path: c.path || null, line: c.line || null, side: c.side || null };
            if (c.author !== 'user' && after(c.created)) {
                items.push(Object.assign({ kind: 'comment', author: c.author, ts: c.created, excerpt: c.body }, where));
            }
            (c.replies || []).forEach(function(r) {
                if (r.author !== 'user' && after(r.created)) {
                    items.push(Object.assign({ kind: 'reply', author: r.author, ts: r.created, excerpt: r.body }, where));
                }
            });
            if (c.status === 'resolved' && c.resolved_by !== 'user' && after(c.resolved_at)) {
                items.push(Object.assign({ kind: 'resolved', author: c.resolved_by, ts: c.resolved_at, excerpt: '' }, where));
            }
        });
        items.sort(function(a, b) { return a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0; });
        return items;
    }

    // Thread ids with agent activity since the previous visit, for the
    // unread marker on the thread heads.
    function unreadThreads(items) {
        var ids = {};
        items.forEach(function(it) { if (it.commentId) ids[it.commentId] = true; });
        return ids;
    }

    function excerpt(text, max) {
        text = (text || '').replace(/\s+/g, ' ').trim();
        max = max || 80;
        return text.length > max ? text.slice(0, max - 1) + '\u2026' : text;
    }

    return {
        emptySelection: emptySelection,
        filterRefs: filterRefs,
        refIsListed: refIsListed,
        mergeBaseNote: mergeBaseNote,
        activitySince: activitySince,
        unreadThreads: unreadThreads,
        excerpt: excerpt,
        pick: pick,
        count: count,
        range: range,
        rangeTarget: rangeTarget,
        summaryText: summaryText,
        title: title,
        refLabel: refLabel,
        singleFlight: singleFlight,
        serialQueue: serialQueue,
        viewedProgress: viewedProgress,
        kindLabel: kindLabel,
        commitsText: commitsText,
        filesText: filesText,
    };
})();

if (typeof module !== 'undefined' && module.exports) {
    module.exports = CompareModel;
}
