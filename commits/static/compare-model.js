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

    function kindLabel(kind) {
        return { commit: 'Commit', range: 'Range', branch: 'Branch', worktree: 'Working tree' }[kind] || kind;
    }

    function commitsText(n) {
        return n + ' commit' + (n === 1 ? '' : 's');
    }

    function filesText(n) {
        return n + ' file' + (n === 1 ? '' : 's');
    }

    return {
        emptySelection: emptySelection,
        pick: pick,
        count: count,
        range: range,
        rangeTarget: rangeTarget,
        summaryText: summaryText,
        title: title,
        refLabel: refLabel,
        kindLabel: kindLabel,
        commitsText: commitsText,
        filesText: filesText,
    };
})();

if (typeof module !== 'undefined' && module.exports) {
    module.exports = CompareModel;
}
