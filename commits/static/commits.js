/* Commit browser: view management, diff rendering, gutter logic.
   Every diff view is a comparison "target": {kind: 'commit', hash} for a
   single commit, {kind: 'compare', base, head, mergebase, worktree} for a
   base..head comparison (range, branch, working tree), or
   {kind: 'review', id} for a saved review, which is a comparison plus its
   review chrome (title, status, viewed files, comments). */

(function() {
    'use strict';

    // State
    let currentView = 'list';
    let currentRepo = '';   // absolute path to current repo
    let commits = [];
    let loadedCount = 0;
    const PAGE_SIZE = 50;
    let searchTimeout = null;
    let currentTarget = null;   // the comparison shown by the diff and file views
    let currentFilePath = null;
    let selectMode = false;     // list rows pick range endpoints instead of opening
    let selection = CompareModel.emptySelection();
    let refsCache = null;       // /api/commits/refs for the compare sheet
    let sheetActiveField = 'head';
    let currentReview = null;   // the loaded review record on a review page
    let reviewMeta = null;      // its comparison detail (files, commits)
    let reviewChanged = new Set();  // paths whose tick was cleared on this load
    let reviewNewCommits = 0;   // commits since the last look, reported once
    let reviewAnchors = {};     // comment id -> current | moved | outdated, from the full load
    let reviewPollTimer = null;
    const REVIEW_POLL_MS = 5000;
    // One review creation at a time (two quick ticks share it), and one
    // mutation of the record at a time (their responses never race).
    const createReviewOnce = CompareModel.singleFlight(createReview);
    const enqueueMutation = CompareModel.serialQueue();
    let gutterLines = [];   // indices into file lines that have gutters
    let gutterIndex = -1;
    let lineHunkMap = [];   // per-line-index: hunk id or null
    let targetLine = null;  // line number to scroll to after file load
    let startupCwd = '';
    let homeDir = '';

    // DOM refs (set in init)
    let listView, diffView, fileView;
    let commitList, listEmpty, loadMoreBtn, listLoading;
    let searchInput, sinceInput, untilInput, commitsFilters;
    let diffMeta, diffContent, diffLoading, fileListToggle, fileListPanel, fileListCount, fileListToggleBtn;
    let fileMeta, fileContent, fileLoading, diffToggle, wrapToggle, gutterFab, fabCounter;
    let repoIndicator, repoPath, repoPickerBtn, noRepoState, pickRepoBtn;
    let compareBtn, selectToggle, worktreeRow, worktreeRowMeta;
    let selectBar, selectSummary, selectClearBtn, selectCompareBtn;
    let compareCommits, compareCommitsBtn, compareCommitsLabel, compareCommitsPanel;
    let sheet, sheetHead, sheetBase, sheetMergebase, sheetError, sheetRefHint, sheetRefList, sheetGoBtn;
    let fileListLabel, reviewChrome, reviewTitle, reviewStatus, reviewRefs, reviewNewCommitsEl, reviewError;
    let reviewCopyBtn, reviewStatusBtn, saveReviewRow, saveReviewBtn;
    let reviewsSection, reviewsOpen, reviewsClosedBtn, reviewsClosedLabel, reviewsClosed;
    let reviewComments, reviewThreads, reviewAddCommentBtn, reviewComposer, reviewComposerText, fileThreadsTop;

    // ---------------------------------------------------------------------------
    // Init
    // ---------------------------------------------------------------------------

    document.addEventListener('DOMContentLoaded', () => {
        const app = document.getElementById('commits-app');
        startupCwd = app.dataset.startupCwd || '';
        homeDir = app.dataset.homeDir || '';

        listView = document.getElementById('commit-list-view');
        diffView = document.getElementById('commit-diff-view');
        fileView = document.getElementById('file-view');
        commitList = document.getElementById('commit-list');
        listEmpty = document.getElementById('list-empty');
        loadMoreBtn = document.getElementById('load-more-btn');
        listLoading = document.getElementById('list-loading');
        searchInput = document.getElementById('search-input');
        sinceInput = document.getElementById('since-input');
        untilInput = document.getElementById('until-input');
        commitsFilters = document.getElementById('commits-filters');
        diffMeta = document.getElementById('diff-meta');
        diffContent = document.getElementById('diff-content');
        diffLoading = document.getElementById('diff-loading');
        fileListToggle = document.getElementById('file-list-toggle');
        fileListPanel = document.getElementById('file-list-panel');
        fileListCount = document.getElementById('file-list-count');
        fileListToggleBtn = document.getElementById('file-list-toggle-btn');
        fileMeta = document.getElementById('file-meta');
        fileContent = document.getElementById('file-content');
        fileLoading = document.getElementById('file-loading');
        diffToggle = document.getElementById('diff-toggle');
        wrapToggle = document.getElementById('wrap-toggle');
        gutterFab = document.getElementById('gutter-fab');
        fabCounter = document.getElementById('fab-counter');
        repoIndicator = document.getElementById('repo-indicator');
        repoPath = document.getElementById('repo-path');
        repoPickerBtn = document.getElementById('repo-picker-btn');
        noRepoState = document.getElementById('no-repo-state');
        pickRepoBtn = document.getElementById('pick-repo-btn');
        compareBtn = document.getElementById('compare-btn');
        selectToggle = document.getElementById('select-toggle');
        worktreeRow = document.getElementById('worktree-row');
        worktreeRowMeta = document.getElementById('worktree-row-meta');
        selectBar = document.getElementById('select-bar');
        selectSummary = document.getElementById('select-summary');
        selectClearBtn = document.getElementById('select-clear-btn');
        selectCompareBtn = document.getElementById('select-compare-btn');
        compareCommits = document.getElementById('compare-commits');
        compareCommitsBtn = document.getElementById('compare-commits-btn');
        compareCommitsLabel = document.getElementById('compare-commits-label');
        compareCommitsPanel = document.getElementById('compare-commits-panel');
        sheet = document.getElementById('compare-sheet');
        sheetHead = document.getElementById('compare-head');
        sheetBase = document.getElementById('compare-base');
        sheetMergebase = document.getElementById('compare-mergebase');
        sheetError = document.getElementById('compare-error');
        sheetRefHint = document.getElementById('compare-ref-hint');
        sheetRefList = document.getElementById('compare-ref-list');
        sheetGoBtn = document.getElementById('compare-go-btn');
        fileListLabel = document.getElementById('file-list-label');
        reviewChrome = document.getElementById('review-chrome');
        reviewTitle = document.getElementById('review-title');
        reviewStatus = document.getElementById('review-status');
        reviewRefs = document.getElementById('review-refs');
        reviewNewCommitsEl = document.getElementById('review-new-commits');
        reviewError = document.getElementById('review-error');
        reviewCopyBtn = document.getElementById('review-copy-btn');
        reviewStatusBtn = document.getElementById('review-status-btn');
        saveReviewRow = document.getElementById('save-review-row');
        saveReviewBtn = document.getElementById('save-review-btn');
        reviewsSection = document.getElementById('reviews-section');
        reviewsOpen = document.getElementById('reviews-open');
        reviewsClosedBtn = document.getElementById('reviews-closed-btn');
        reviewsClosedLabel = document.getElementById('reviews-closed-label');
        reviewsClosed = document.getElementById('reviews-closed');
        reviewComments = document.getElementById('review-comments');
        reviewThreads = document.getElementById('review-threads');
        reviewAddCommentBtn = document.getElementById('review-add-comment-btn');
        reviewComposer = document.getElementById('review-composer');
        reviewComposerText = document.getElementById('review-composer-text');
        fileThreadsTop = document.getElementById('file-threads-top');

        // Event listeners
        searchInput.addEventListener('input', debounceSearch);
        sinceInput.addEventListener('change', resetAndLoad);
        untilInput.addEventListener('change', resetAndLoad);
        loadMoreBtn.addEventListener('click', loadMore);
        document.getElementById('diff-back-btn').addEventListener('click', () => navigateTo('list'));
        document.getElementById('file-back-btn').addEventListener('click', () => {
            if (currentTarget) navigateTo('diff', currentTarget);
            else navigateTo('list');
        });
        selectToggle.addEventListener('click', toggleSelectMode);
        selectClearBtn.addEventListener('click', clearSelection);
        selectCompareBtn.addEventListener('click', openSelectedRange);
        worktreeRow.addEventListener('click', () => navigateTo('diff', worktreeTarget()));
        worktreeRow.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigateTo('diff', worktreeTarget()); }
        });
        compareCommitsBtn.addEventListener('click', toggleCompareCommits);
        compareBtn.addEventListener('click', openCompareSheet);
        document.getElementById('compare-sheet-close').addEventListener('click', closeCompareSheet);
        sheet.querySelector('.picker-overlay').addEventListener('click', closeCompareSheet);
        document.getElementById('compare-worktree-btn').addEventListener('click', () => {
            closeCompareSheet();
            navigateTo('diff', worktreeTarget());
        });
        sheetGoBtn.addEventListener('click', submitCompareSheet);
        for (const [input, field] of [[sheetHead, 'head'], [sheetBase, 'base']]) {
            input.addEventListener('focus', () => { sheetActiveField = field; renderRefList(); });
            input.addEventListener('input', renderRefList);
            input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitCompareSheet(); });
        }
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && sheet.style.display !== 'none') {
                e.stopPropagation();
                closeCompareSheet();
            }
        });
        saveReviewBtn.addEventListener('click', () => ensureReview());
        reviewCopyBtn.addEventListener('click', copyForAgent);
        reviewStatusBtn.addEventListener('click', toggleReviewStatus);
        reviewTitle.addEventListener('change', saveReviewTitle);
        reviewTitle.addEventListener('keydown', (e) => { if (e.key === 'Enter') reviewTitle.blur(); });
        reviewsClosedBtn.addEventListener('click', () => {
            const open = reviewsClosed.style.display === 'none';
            reviewsClosed.style.display = open ? '' : 'none';
            reviewsClosedBtn.classList.toggle('open', open);
            reviewsClosedBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        });
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) pollReview();
        });
        window.addEventListener('resize', sizeInlineThreads);
        reviewAddCommentBtn.addEventListener('click', () => {
            reviewComposer.style.display = '';
            reviewComposerText.focus();
        });
        document.getElementById('review-composer-cancel').addEventListener('click', () => {
            reviewComposer.style.display = 'none';
            reviewComposerText.value = '';
        });
        document.getElementById('review-composer-submit').addEventListener('click', async () => {
            const ok = await submitComment({ body: reviewComposerText.value });
            if (ok) {
                reviewComposerText.value = '';
                reviewComposer.style.display = 'none';
            }
        });
        fileListToggleBtn.addEventListener('click', toggleFileList);
        diffToggle.addEventListener('click', toggleDiffMode);
        wrapToggle.addEventListener('click', toggleWrap);
        document.getElementById('fab-next').addEventListener('click', jumpToNextGutter);
        document.getElementById('fab-prev').addEventListener('click', jumpToPrevGutter);
        repoPickerBtn.addEventListener('click', openPicker);
        pickRepoBtn.addEventListener('click', openPicker);

        // Handle browser back/forward
        window.addEventListener('popstate', handlePopState);

        // Init folder picker
        FolderPicker.init({
            homeDir: homeDir,
            onSelect: selectRepo,
        });

        // Smart default chain
        resolveDefaultRepo();
    });

    // ---------------------------------------------------------------------------
    // Smart default chain (R6)
    // ---------------------------------------------------------------------------

    async function resolveDefaultRepo() {
        // 0. A review URL names its repository itself: the record's stored
        //    root wins over any ?repo=, saved repo or terminal directory.
        const reviewId = reviewIdFromPath(window.location.pathname);
        if (reviewId) {
            const rv = await API.get('/api/commits/reviews/' + reviewId + '?since=');
            if (rv && rv.review && rv.review.repo) {
                selectRepo(rv.review.repo);
                return;
            }
            // No such review: route anyway so the page says so
            routeFromUrl();
            return;
        }

        // 1. Check URL ?repo= param
        const urlParams = new URLSearchParams(window.location.search);
        const urlRepo = urlParams.get('repo');
        if (urlRepo) {
            selectRepo(urlRepo);
            return;
        }

        // 2. Check localStorage
        const saved = localStorage.getItem('merlin-commits-repo');
        if (saved) {
            // Validate it's still a git repo
            const data = await API.get('/api/commits?repo=' + encodeURIComponent(saved) + '&limit=1');
            if (data && data.length >= 0) {
                selectRepo(saved, true);
                return;
            }
            localStorage.removeItem('merlin-commits-repo');
        }

        // 3. Check terminal CWD
        try {
            const cwd = await API.get('/api/terminal/cwd');
            if (cwd && cwd.is_git_repo && cwd.repo_root) {
                selectRepo(cwd.repo_root);
                return;
            }
        } catch (_) {}

        // 4. Try startup CWD
        if (startupCwd && startupCwd !== '/') {
            try {
                const data = await API.get('/api/commits?repo=' + encodeURIComponent(startupCwd) + '&limit=1');
                if (data && data.length >= 0) {
                    selectRepo(startupCwd, true);
                    return;
                }
            } catch (_) {}
        }

        // 5. Empty state
        showEmptyRepoState();
    }

    function selectRepo(path, skipUrlUpdate) {
        currentRepo = path;
        MerlinPageTitle.set('commits', MerlinPageTitle.pathContext(path));
        localStorage.setItem('merlin-commits-repo', path);
        updateRepoIndicator();

        // Reset commit list so it reloads for the new repo
        commits = [];
        loadedCount = 0;
        commitList.innerHTML = '';

        if (!skipUrlUpdate) {
            const url = new URL(window.location);
            url.searchParams.set('repo', path);
            if (window.location.href !== url.href) {
                history.replaceState(null, '', url);
            }
        }

        // Route from URL (handles diff/file deep links)
        routeFromUrl();
    }

    function updateRepoIndicator() {
        repoIndicator.style.display = '';
        noRepoState.style.display = 'none';
        commitsFilters.style.display = '';
        compareBtn.style.display = '';
        repoPath.textContent = shortenPath(currentRepo);
    }

    function showEmptyRepoState() {
        MerlinPageTitle.set('commits');
        repoIndicator.style.display = 'none';
        noRepoState.style.display = '';
        commitsFilters.style.display = 'none';
        compareBtn.style.display = 'none';
        worktreeRow.style.display = 'none';
        listLoading.style.display = 'none';
    }

    function openPicker() {
        FolderPicker.open(currentRepo || homeDir);
    }

    function shortenPath(p) {
        if (homeDir && p.startsWith(homeDir)) {
            return '~' + p.slice(homeDir.length);
        }
        return p;
    }

    // ---------------------------------------------------------------------------
    // Repo query param helper
    // ---------------------------------------------------------------------------

    function repoParam() {
        if (!currentRepo) return '';
        return '&repo=' + encodeURIComponent(currentRepo);
    }

    function repoParamFirst() {
        if (!currentRepo) return '';
        return '?repo=' + encodeURIComponent(currentRepo);
    }

    // ---------------------------------------------------------------------------
    // Targets and URLs
    // ---------------------------------------------------------------------------

    function commitTarget(hash) {
        return { kind: 'commit', hash: hash };
    }

    function reviewIdFromPath(path) {
        const m = path.match(/^\/commits\/reviews\/([0-9a-f]{8})(?:\/file\/.+)?$/);
        return m ? m[1] : null;
    }

    function worktreeTarget() {
        return { kind: 'compare', base: '', head: '', mergebase: false, worktree: true };
    }

    // Query parameters that identify a comparison (the URL is the only state).
    function targetParams(target) {
        const params = new URLSearchParams();
        if (currentRepo) params.set('repo', currentRepo);
        if (target && target.kind === 'compare') {
            if (target.worktree) {
                params.set('worktree', '1');
                if (target.base) params.set('base', target.base);
            } else {
                params.set('base', target.base);
                params.set('head', target.head);
                if (target.mergebase) params.set('mergebase', '1');
            }
        }
        return params;
    }

    function withQuery(path, params) {
        const q = params.toString();
        return q ? path + '?' + q : path;
    }

    function pageUrl(view, target, filePath) {
        if (view === 'list') return withQuery('/commits', targetParams(null));
        let base;
        if (target.kind === 'commit') base = '/commits/' + target.hash;
        else if (target.kind === 'review') base = '/commits/reviews/' + target.id;
        else base = '/commits/compare';
        const path = view === 'file' ? base + '/file/' + filePath : base;
        return withQuery(path, targetParams(target));
    }

    // The comparison behind a commit or compare target, for saving it as a
    // review: a commit is commit^..commit.
    function compareTargetOf(target) {
        if (target.kind === 'compare') return target;
        if (target.kind === 'commit') {
            return { kind: 'compare', base: target.hash + '^', head: target.hash, mergebase: false, worktree: false };
        }
        return null;
    }

    function apiUrl(what, target, filePath) {
        if (target.kind === 'review') {
            // Keyed by the review id alone: the server resolves the diff in
            // the review's stored repository, no query can point elsewhere.
            const base = '/api/commits/reviews/' + target.id;
            if (what === 'diff') return base + '/diff';
            if (what === 'file') return base + '/file/' + encodeURIComponent(filePath).replace(/%2F/g, '/');
            return base;
        }
        const base = target.kind === 'commit' ? '/api/commits/' + target.hash : '/api/commits/compare';
        let path = base;
        if (what === 'diff') path = base + '/diff';
        if (what === 'file') path = base + '/file/' + encodeURIComponent(filePath).replace(/%2F/g, '/');
        return withQuery(path, targetParams(target));
    }

    function targetFromSearch(search) {
        const params = new URLSearchParams(search);
        if (params.get('worktree') === '1') {
            const t = worktreeTarget();
            t.base = params.get('base') || '';
            return t;
        }
        return {
            kind: 'compare',
            base: params.get('base') || '',
            head: params.get('head') || '',
            mergebase: params.get('mergebase') === '1',
            worktree: false,
        };
    }

    // ---------------------------------------------------------------------------
    // Routing
    // ---------------------------------------------------------------------------

    function routeFromUrl() {
        const path = window.location.pathname;
        const search = window.location.search;
        const m_rev_file = path.match(/^\/commits\/reviews\/([0-9a-f]{8})\/file\/(.+)$/);
        const m_rev = path.match(/^\/commits\/reviews\/([0-9a-f]{8})$/);
        const m_cmp_file = path.match(/^\/commits\/compare\/file\/(.+)$/);
        const m_cmp = path.match(/^\/commits\/compare$/);
        const m_file = path.match(/^\/commits\/([0-9a-f]+)\/file\/(.+)$/);
        const m_diff = path.match(/^\/commits\/([0-9a-f]+)$/);

        // Read repo from URL if not already set
        if (!currentRepo) {
            const urlParams = new URLSearchParams(search);
            const r = urlParams.get('repo');
            if (r) currentRepo = r;
        }

        if (m_rev_file) {
            const target = { kind: 'review', id: m_rev_file[1] };
            showFileView(target, decodeURIComponent(m_rev_file[2]), false);
        } else if (m_rev) {
            showDiffView({ kind: 'review', id: m_rev[1] }, false);
        } else if (m_cmp_file) {
            const target = targetFromSearch(search);
            showDiffView(target, false);
            showFileView(target, decodeURIComponent(m_cmp_file[1]), false);
        } else if (m_cmp) {
            showDiffView(targetFromSearch(search), false);
        } else if (m_file) {
            showDiffView(commitTarget(m_file[1]), false);
            showFileView(commitTarget(m_file[1]), decodeURIComponent(m_file[2]), false);
        } else if (m_diff) {
            showDiffView(commitTarget(m_diff[1]), false);
        } else {
            showListView(false);
        }
    }

    function handlePopState() {
        routeFromUrl();
    }

    function navigateTo(view, target, filePath) {
        if (view === 'list') {
            showListView(true);
        } else if (view === 'diff') {
            showDiffView(target, true);
        } else if (view === 'file') {
            showFileView(target, filePath, true);
        }
    }

    function pushUrl(url) {
        if (window.location.pathname + window.location.search !== url) {
            history.pushState(null, '', url);
        }
    }

    // ---------------------------------------------------------------------------
    // View switching
    // ---------------------------------------------------------------------------

    function showListView(pushState) {
        currentView = 'list';
        stopReviewPoll();
        listView.style.display = '';
        diffView.style.display = 'none';
        fileView.style.display = 'none';
        if (pushState) pushUrl(pageUrl('list'));
        if (currentRepo && commits.length === 0) {
            loadCommits();
        }
    }

    function showDiffView(target, pushState) {
        currentView = 'diff';
        currentTarget = target;
        listView.style.display = 'none';
        diffView.style.display = '';
        fileView.style.display = 'none';
        if (pushState) pushUrl(pageUrl('diff', target));
        loadDiff(target);
    }

    function showFileView(target, filePath, pushState) {
        currentView = 'file';
        stopReviewPoll();
        currentTarget = target;
        currentFilePath = filePath;
        listView.style.display = 'none';
        diffView.style.display = 'none';
        fileView.style.display = '';
        if (pushState) pushUrl(pageUrl('file', target, filePath));
        loadFile(target, filePath);
    }

    // ---------------------------------------------------------------------------
    // Commit List (View 1)
    // ---------------------------------------------------------------------------

    async function loadCommits(append) {
        if (!currentRepo) return;

        if (!append) {
            loadedCount = 0;
            commits = [];
            commitList.innerHTML = '';
        }

        listLoading.style.display = '';
        loadMoreBtn.style.display = 'none';
        listEmpty.style.display = 'none';
        if (!append) {
            loadWorktreeRow();
            loadReviewsList();
        }

        const params = new URLSearchParams();
        params.set('skip', loadedCount);
        params.set('limit', PAGE_SIZE);
        if (currentRepo) params.set('repo', currentRepo);
        if (searchInput.value.trim()) params.set('search', searchInput.value.trim());
        if (sinceInput.value) params.set('since', sinceInput.value);
        if (untilInput.value) params.set('until', untilInput.value);

        const data = await API.get('/api/commits?' + params);
        listLoading.style.display = 'none';

        if (!data) return;

        if (!append && data.length === 0) {
            listEmpty.style.display = '';
            return;
        }

        commits = commits.concat(data);
        loadedCount += data.length;

        for (const c of data) {
            commitList.appendChild(renderCommitItem(c));
        }
        renderSelection();

        if (data.length >= PAGE_SIZE) {
            loadMoreBtn.style.display = '';
        }
    }

    function renderCommitItem(c) {
        const el = document.createElement('div');
        el.className = 'commit-item';
        el.dataset.hash = c.hash;
        el.addEventListener('click', () => {
            if (selectMode) pickRow(c.hash);
            else navigateTo('diff', commitTarget(c.hash));
        });

        const statsHtml = (c.insertions || c.deletions)
            ? `<div class="commit-stats">` +
              (c.insertions ? `<span class="stat-add">+${c.insertions}</span>` : '') +
              (c.deletions ? `<span class="stat-del">-${c.deletions}</span>` : '') +
              `</div>`
            : '';

        el.innerHTML =
            `<span class="commit-check" aria-hidden="true"></span>` +
            `<span class="commit-hash">${esc(c.short)}</span>` +
            `<div class="commit-info">` +
                `<div class="commit-message">${esc(c.message)}</div>` +
                `<div class="commit-details">${esc(c.author)} · ${timeAgo(c.date)}</div>` +
            `</div>` +
            statsHtml;

        return el;
    }

    function debounceSearch() {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => resetAndLoad(), 300);
    }

    function resetAndLoad() {
        loadCommits(false);
    }

    function loadMore() {
        loadCommits(true);
    }

    // ---------------------------------------------------------------------------
    // Working tree row (pinned above the list when the tree is dirty)
    // ---------------------------------------------------------------------------

    async function loadWorktreeRow() {
        if (!currentRepo) return;
        let data = null;
        try {
            data = await API.get(apiUrl('detail', worktreeTarget()));
        } catch (_) {}
        if (!data || !data.files || data.files.length === 0) {
            worktreeRow.style.display = 'none';
            return;
        }
        let ins = 0, del = 0;
        for (const f of data.files) { ins += f.insertions || 0; del += f.deletions || 0; }
        worktreeRowMeta.innerHTML =
            `<span>${CompareModel.filesText(data.files.length)}</span>` +
            (ins ? ` · <span class="stat-add">+${ins}</span>` : '') +
            (del ? ` <span class="stat-del">-${del}</span>` : '');
        worktreeRow.style.display = '';
    }

    // ---------------------------------------------------------------------------
    // Reviews list (open reviews, closed ones collapsed)
    // ---------------------------------------------------------------------------

    async function apiJson(method, url, body) {
        const resp = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        if (resp.status === 401) {
            window.location.reload();
            return null;
        }
        try { return await resp.json(); } catch (_) { return null; }
    }

    async function loadReviewsList() {
        if (!currentRepo) return;
        let data = null;
        try { data = await API.get('/api/commits/reviews' + repoParamFirst()); } catch (_) {}
        if (!data || data.detail || !Array.isArray(data) || data.length === 0) {
            reviewsSection.style.display = 'none';
            return;
        }
        const open = data.filter(r => !r.error && r.status !== 'closed');
        const closed = data.filter(r => !r.error && r.status === 'closed');
        reviewsOpen.innerHTML = '';
        reviewsClosed.innerHTML = '';
        for (const r of open) reviewsOpen.appendChild(renderReviewItem(r));
        for (const r of closed) reviewsClosed.appendChild(renderReviewItem(r));
        reviewsClosedBtn.style.display = closed.length ? '' : 'none';
        reviewsClosedLabel.textContent = 'Closed (' + closed.length + ')';
        reviewsSection.style.display = (open.length || closed.length) ? '' : 'none';
    }

    function renderReviewItem(r) {
        const el = document.createElement('div');
        el.className = 'review-item' + (r.status === 'closed' ? ' closed' : '');
        el.dataset.id = r.id;
        const meta = [CompareModel.kindLabel(r.kind)];
        if (r.open_comments) meta.push(r.open_comments + ' open');
        meta.push(timeAgo(r.updated));
        el.innerHTML =
            `<span class="review-item-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m9 15 2 2 4-4"/></svg></span>` +
            `<span class="review-item-title">${esc(r.title)}</span>` +
            `<span class="review-item-meta">${esc(meta.join(' · '))}</span>`;
        el.addEventListener('click', () => navigateTo('diff', { kind: 'review', id: r.id }));
        return el;
    }

    // ---------------------------------------------------------------------------
    // Select mode: two taps pick a range of commits to compare
    // ---------------------------------------------------------------------------

    function toggleSelectMode() {
        selectMode = !selectMode;
        selection = CompareModel.emptySelection();
        selectToggle.classList.toggle('active', selectMode);
        selectToggle.setAttribute('aria-pressed', selectMode ? 'true' : 'false');
        commitList.classList.toggle('selecting', selectMode);
        listView.classList.toggle('has-select-bar', selectMode);
        selectBar.style.display = selectMode ? '' : 'none';
        renderSelection();
    }

    function clearSelection() {
        selection = CompareModel.emptySelection();
        renderSelection();
    }

    function pickRow(hash) {
        selection = CompareModel.pick(selection, hash);
        renderSelection();
    }

    function selectedRange() {
        return CompareModel.range(selection, commits.map(c => c.hash));
    }

    function renderSelection() {
        const r = selectMode ? selectedRange() : null;
        const inRange = new Set(r ? r.hashes : []);
        for (const el of commitList.querySelectorAll('.commit-item')) {
            const h = el.dataset.hash;
            el.classList.toggle('selected', h === selection.a || h === selection.b);
            el.classList.toggle('in-range', inRange.has(h));
        }
        if (!selectMode) return;
        selectSummary.textContent = CompareModel.summaryText(r ? r.count : 0);
        selectCompareBtn.disabled = !r;
    }

    function openSelectedRange() {
        const r = selectedRange();
        if (!r) return;
        navigateTo('diff', CompareModel.rangeTarget(r));
    }

    // ---------------------------------------------------------------------------
    // Compare sheet: head and base fields with the branch lists
    // ---------------------------------------------------------------------------

    async function openCompareSheet() {
        sheetError.style.display = 'none';
        sheet.style.display = '';
        document.body.style.overflow = 'hidden';
        sheetRefList.innerHTML = '<div class="picker-loading">Loading branches...</div>';
        refsCache = null;
        const refs = await API.get('/api/commits/refs' + repoParamFirst());
        if (!refs || refs.detail) {
            sheetRefList.innerHTML = `<div class="picker-empty">${esc(refs && refs.detail ? refs.detail : 'Could not list branches')}</div>`;
            return;
        }
        refsCache = refs;
        sheetHead.value = refs.current || '';
        sheetBase.value = refs.default_base || '';
        sheetMergebase.checked = true;
        sheetActiveField = 'head';
        renderRefList();
        sheetHead.focus();
    }

    function closeCompareSheet() {
        sheet.style.display = 'none';
        document.body.style.overflow = '';
    }

    function renderRefList() {
        if (!refsCache) return;
        const input = sheetActiveField === 'head' ? sheetHead : sheetBase;
        const q = input.value.trim().toLowerCase();
        sheetRefHint.textContent = 'Branches for ' + sheetActiveField + (q ? ' matching "' + input.value.trim() + '"' : '');
        sheetRefList.innerHTML = '';
        const groups = [['local', refsCache.local || []], ['remote', refsCache.remote || []]];
        let shown = 0;
        for (const [label, names] of groups) {
            const matches = names.filter(n => !q || n.toLowerCase().includes(q));
            if (matches.length === 0) continue;
            const head = document.createElement('div');
            head.className = 'compare-ref-group';
            head.textContent = label;
            sheetRefList.appendChild(head);
            for (const name of matches) {
                const item = document.createElement('div');
                item.className = 'picker-item compare-ref-item';
                item.setAttribute('role', 'button');
                item.tabIndex = 0;
                item.innerHTML =
                    `<span class="picker-item-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg></span>` +
                    `<span class="picker-item-name">${esc(name)}</span>` +
                    (name === refsCache.current ? '<span class="compare-ref-current">current</span>' : '');
                const choose = () => {
                    input.value = name;
                    // Move on to the other field after filling this one
                    if (sheetActiveField === 'head') { sheetActiveField = 'base'; sheetBase.focus(); }
                    else renderRefList();
                };
                item.addEventListener('click', choose);
                item.addEventListener('keydown', (e) => { if (e.key === 'Enter') choose(); });
                sheetRefList.appendChild(item);
                shown++;
            }
        }
        if (shown === 0) {
            sheetRefList.innerHTML = '<div class="picker-empty">No branch matches. Any ref git understands works as typed.</div>';
        }
    }

    function submitCompareSheet() {
        const head = sheetHead.value.trim();
        const base = sheetBase.value.trim();
        if (!head || !base) {
            sheetError.textContent = 'Both head and base are needed';
            sheetError.style.display = '';
            return;
        }
        closeCompareSheet();
        navigateTo('diff', { kind: 'compare', base: base, head: head, mergebase: sheetMergebase.checked, worktree: false });
    }

    // ---------------------------------------------------------------------------
    // Commit Diff (View 2)
    // ---------------------------------------------------------------------------

    function resetDiffView() {
        diffContent.innerHTML = '';
        diffLoading.style.display = '';
        fileListToggle.style.display = 'none';
        compareCommits.style.display = 'none';
        reviewChrome.style.display = 'none';
        saveReviewRow.style.display = 'none';
        reviewComments.style.display = 'none';
        reviewThreads.innerHTML = '';
        reviewComposer.style.display = 'none';
        diffMeta.innerHTML = '';
        stopReviewPoll();
        if (!currentTarget || currentTarget.kind !== 'review') {
            currentReview = null;
            reviewMeta = null;
            reviewChanged = new Set();
            reviewNewCommits = 0;
            reviewAnchors = {};
        }
    }

    async function loadDiff(target) {
        resetDiffView();
        if (target.kind === 'review') {
            await loadReview(target.id);
            return;
        }

        // Load metadata and diff in parallel
        const [meta, diff] = await Promise.all([
            API.get(apiUrl('detail', target)),
            API.get(apiUrl('diff', target)),
        ]);

        diffLoading.style.display = 'none';
        if (!meta || !diff) return;
        if (meta.detail || diff.detail) {
            diffMeta.innerHTML = `<div class="commit-meta-message">Cannot compare</div>` +
                `<div class="commit-meta-info diff-error">${esc(meta.detail || diff.detail)}</div>`;
            return;
        }

        // Render header
        if (target.kind === 'commit') {
            diffMeta.innerHTML =
                `<div class="commit-meta-message">${esc(meta.message)}</div>` +
                `<div class="commit-meta-info">` +
                    `<span class="commit-meta-hash">${esc(meta.short)}</span> · ` +
                    `${esc(meta.author)} · ${timeAgo(meta.date)}` +
                `</div>`;
        } else {
            renderCompareHeader(meta);
        }

        // File list
        renderFilesPanel(meta.files || []);
        if (!(meta.files && meta.files.length) && target.kind === 'compare') {
            diffContent.innerHTML = '<div class="empty-state"><p>Nothing to compare: no changes between these points</p></div>';
        }

        // Render diff sections
        for (const file of diff.files) {
            diffContent.appendChild(renderDiffFile(file, target));
        }
        reviewMeta = target.kind === 'compare' ? meta : null;
        saveReviewRow.style.display = '';
        reviewComments.style.display = '';
        applyViewedState();
        renderAllThreads();
    }

    // The files panel: one row per file with its viewed checkbox, status,
    // path, a "changed" marker when its tick was cleared, and stats.
    function renderFilesPanel(files) {
        fileListPanel.innerHTML = '';
        if (!files || files.length === 0) {
            fileListToggle.style.display = 'none';
            return;
        }
        fileListToggle.style.display = '';
        for (const f of files) {
            const item = document.createElement('div');
            item.className = 'file-list-item';
            item.dataset.path = f.path;
            item.innerHTML =
                viewedCheckHtml(f.path) +
                `<span class="file-status file-status-${esc(f.status)}">${esc(f.status)}</span>` +
                `<span class="file-list-path">${esc(f.path)}</span>` +
                `<span class="file-changed-marker" style="display:none">changed</span>` +
                `<span class="file-list-stats">` +
                    (f.insertions ? `<span class="stat-add">+${f.insertions}</span>` : '') +
                    (f.deletions ? `<span class="stat-del">-${f.deletions}</span>` : '') +
                `</span>`;
            const path = f.path;
            item.addEventListener('click', (e) => {
                if (e.target.closest('.viewed-check')) return;
                // Scroll to the file section in the diff
                const section = document.getElementById('diff-file-' + CSS.escape(path));
                if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
            });
            bindViewedCheck(item, path);
            fileListPanel.appendChild(item);
        }
        updateFilesLabel();
    }

    function viewedCheckHtml(path) {
        return `<label class="viewed-check" title="Mark as viewed"><input type="checkbox" aria-label="Viewed: ${esc(path)}"></label>`;
    }

    function bindViewedCheck(container, path) {
        const input = container.querySelector('.viewed-check input');
        if (!input) return;
        input.addEventListener('click', (e) => e.stopPropagation());
        input.addEventListener('change', () => toggleViewed(path, input.checked));
    }

    function updateFilesLabel() {
        const n = fileListPanel.querySelectorAll('.file-list-item').length;
        if (currentTarget && currentTarget.kind === 'review' && currentReview) {
            const k = fileListPanel.querySelectorAll('.file-list-item.viewed').length;
            fileListLabel.textContent = CompareModel.viewedProgress(k, n);
        } else {
            fileListLabel.textContent = CompareModel.filesText(n) + ' changed';
        }
    }

    // The comparison header: what is compared, the resolved hashes, the
    // commit count, and the included commits collapsed under it.
    function renderCompareHeader(meta) {
        const parts = [CompareModel.kindLabel(meta.kind)];
        if (meta.worktree) {
            parts.push(`against ${esc(meta.base)} <span class="commit-meta-hash">${esc(meta.base_short)}</span>`);
        } else {
            // Each ref with its own resolved hash. In merge-base mode the diff
            // runs from the merge base, which is reported on its own.
            parts.push(`${esc(CompareModel.refLabel(meta.base))} <span class="commit-meta-hash">${esc(meta.base_short)}</span>` +
                ` .. ${esc(CompareModel.refLabel(meta.head))} <span class="commit-meta-hash">${esc(meta.head_short)}</span>`);
            if (meta.mergebase) parts.push(`merge base <span class="commit-meta-hash">${esc(meta.merge_base_short)}</span>`);
            parts.push(CompareModel.commitsText(meta.commit_count));
        }
        diffMeta.innerHTML =
            `<div class="commit-meta-message">${esc(CompareModel.title(meta))}</div>` +
            `<div class="commit-meta-info">${parts.join(' · ')}</div>`;
        renderIncludedCommits(meta);
    }

    function compareRefsHtml(meta) {
        const parts = [CompareModel.kindLabel(meta.kind)];
        if (meta.worktree) {
            parts.push(`against ${esc(meta.base)} <span class="commit-meta-hash">${esc(meta.base_short)}</span>`);
        } else {
            parts.push(`${esc(CompareModel.refLabel(meta.base))} <span class="commit-meta-hash">${esc(meta.base_short)}</span>` +
                ` .. ${esc(CompareModel.refLabel(meta.head))} <span class="commit-meta-hash">${esc(meta.head_short)}</span>`);
            if (meta.mergebase) parts.push(`merge base <span class="commit-meta-hash">${esc(meta.merge_base_short)}</span>`);
            parts.push(CompareModel.commitsText(meta.commit_count));
        }
        return parts.join(' · ');
    }

    function renderIncludedCommits(meta) {
        compareCommits.style.display = 'none';
        if (meta.worktree || !meta.commits || meta.commits.length === 0) return;
        compareCommitsLabel.textContent = CompareModel.commitsText(meta.commit_count) +
            (meta.commit_count > meta.commits.length ? ` (first ${meta.commits.length} shown)` : '');
        compareCommitsPanel.innerHTML = '';
        compareCommitsPanel.style.display = 'none';
        compareCommitsBtn.classList.remove('open');
        for (const c of meta.commits) {
            const item = document.createElement('div');
            item.className = 'compare-commit-item';
            item.innerHTML =
                `<span class="commit-hash">${esc(c.short)}</span>` +
                `<span class="compare-commit-message">${esc(c.message)}</span>` +
                `<span class="compare-commit-meta">${esc(c.author)} · ${timeAgo(c.date)}</span>`;
            item.addEventListener('click', () => navigateTo('diff', commitTarget(c.hash)));
            compareCommitsPanel.appendChild(item);
        }
        compareCommits.style.display = '';
    }

    function toggleCompareCommits() {
        const open = compareCommitsPanel.style.display === 'none';
        compareCommitsPanel.style.display = open ? '' : 'none';
        compareCommitsBtn.classList.toggle('open', open);
    }

    function renderDiffFile(file, target) {
        const section = document.createElement('div');
        section.className = 'diff-file-section';
        section.id = 'diff-file-' + file.path;
        section.dataset.path = file.path;

        // Header: viewed checkbox, path, then the Full file button. Sticky
        // while the file scrolls (commits.css). A viewed section collapses
        // to its header, and tapping the header expands it again.
        const header = document.createElement('div');
        header.className = 'diff-file-header';
        header.innerHTML = viewedCheckHtml(file.path);
        bindViewedCheck(header, file.path);
        header.addEventListener('click', (e) => {
            if (e.target.closest('.viewed-check, .full-file-btn')) return;
            if (section.classList.contains('viewed')) section.classList.toggle('expanded');
        });

        const pathSpan = document.createElement('span');
        pathSpan.className = 'diff-file-path';
        pathSpan.textContent = file.path;
        header.appendChild(pathSpan);

        const count = document.createElement('span');
        count.className = 'diff-file-count';
        header.appendChild(count);

        if (file.status !== 'D') {
            const btn = document.createElement('button');
            btn.className = 'full-file-btn';
            btn.textContent = 'Full file';
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                navigateTo('file', target, file.path);
            });
            header.appendChild(btn);
        }

        section.appendChild(header);

        // Binary file notice
        if (file.binary) {
            const notice = document.createElement('div');
            notice.className = 'diff-binary-notice';
            notice.textContent = 'Binary file';
            section.appendChild(notice);
            return section;
        }

        // Hunks
        if (file.hunks && file.hunks.length > 0) {
            const table = document.createElement('table');
            table.className = 'diff-table';
            const tbody = document.createElement('tbody');

            for (const hunk of file.hunks) {
                // Parse new-file start line from hunk header (e.g. @@ -401,33 +403,42 @@)
                const hunkNewStart = (function() {
                    const m = hunk.header.match(/\+(\d+)/);
                    return m ? parseInt(m[1], 10) : null;
                })();

                // Hunk header row — click to open file view at this hunk
                const hdr = document.createElement('tr');
                hdr.className = 'diff-hunk-header';
                if (file.status !== 'D' && hunkNewStart) {
                    hdr.style.cursor = 'pointer';
                    hdr.addEventListener('click', () => {
                        targetLine = hunkNewStart;
                        navigateTo('file', target, file.path);
                    });
                }
                hdr.innerHTML = `<td colspan="3">${esc(hunk.header)}${file.status !== 'D' && hunkNewStart ? '<svg class="hunk-view-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 7h10v10"/><path d="M7 17 17 7"/></svg>' : ''}</td>`;
                tbody.appendChild(hdr);

                for (const line of hunk.lines) {
                    const tr = document.createElement('tr');
                    tr.className = 'diff-line-' + (line.type === 'add' ? 'add' : line.type === 'del' ? 'del' : 'ctx');
                    // A deleted line lives on the old side, added and context
                    // lines on the new side (decision 11).
                    tr.dataset.side = line.type === 'del' ? 'old' : 'new';
                    tr.dataset.line = line.type === 'del' ? line.old_no : line.new_no;

                    const oldNo = document.createElement('td');
                    oldNo.className = 'diff-line-no';
                    oldNo.textContent = line.old_no != null ? line.old_no : '';

                    const newNo = document.createElement('td');
                    newNo.className = 'diff-line-no';
                    newNo.textContent = line.new_no != null ? line.new_no : '';

                    const content = document.createElement('td');
                    content.className = 'diff-line-content';
                    const prefix = line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' ';
                    content.textContent = prefix + line.content;

                    tr.appendChild(oldNo);
                    tr.appendChild(newNo);
                    tr.appendChild(content);
                    armCommentCells(tr, [oldNo, newNo], file.path);
                    tbody.appendChild(tr);
                }
            }

            table.appendChild(tbody);
            const scrollWrap = document.createElement('div');
            scrollWrap.className = 'diff-table-scroll';
            scrollWrap.appendChild(table);
            section.appendChild(scrollWrap);
        }

        return section;
    }

    // ---------------------------------------------------------------------------
    // Review page: the comparison plus its chrome, viewed files, the poll
    // ---------------------------------------------------------------------------

    async function loadReview(id) {
        const data = await API.get('/api/commits/reviews/' + id);
        diffLoading.style.display = 'none';
        if (!data) return;
        if (data.detail) {
            diffMeta.innerHTML = `<div class="commit-meta-message">Review not found</div>` +
                `<div class="commit-meta-info diff-error">${esc(data.detail)}</div>`;
            return;
        }
        if (!currentTarget || currentTarget.kind !== 'review' || currentTarget.id !== id) return;
        currentReview = data.review;
        reviewMeta = data.comparison;
        reviewChanged = new Set(data.changed_since_viewed || []);
        reviewNewCommits = data.new_commits || 0;
        reviewAnchors = data.anchors || {};
        diffMeta.innerHTML = `<div class="commit-meta-info">Review · ${esc(CompareModel.kindLabel(currentReview.kind))}</div>`;
        renderReviewChrome(data.error);
        reviewComments.style.display = '';
        if (!data.comparison) {
            renderAllThreads();
            return;
        }

        diffLoading.style.display = '';
        const diff = await API.get(apiUrl('diff', currentTarget));
        diffLoading.style.display = 'none';
        if (!diff) return;
        if (!currentTarget || currentTarget.kind !== 'review' || currentTarget.id !== id) return;
        if (diff.detail) {
            reviewError.textContent = diff.detail;
            reviewError.style.display = '';
            return;
        }
        renderIncludedCommits(data.comparison);
        renderFilesPanel(data.comparison.files || []);
        if (!(data.comparison.files && data.comparison.files.length)) {
            diffContent.innerHTML = '<div class="empty-state"><p>Nothing to compare: no changes between these points</p></div>';
        }
        for (const file of diff.files) {
            diffContent.appendChild(renderDiffFile(file, currentTarget));
        }
        applyViewedState();
        renderAllThreads();
        startReviewPoll();
    }

    function renderReviewChrome(error) {
        if (!currentReview) return;
        reviewChrome.style.display = '';
        saveReviewRow.style.display = 'none';
        if (document.activeElement !== reviewTitle) reviewTitle.value = currentReview.title || '';
        const closed = currentReview.status === 'closed';
        reviewStatus.textContent = closed ? 'Closed' : 'Open';
        reviewStatus.classList.toggle('closed', closed);
        reviewStatusBtn.textContent = closed ? 'Reopen' : 'Close review';
        if (reviewMeta && reviewMeta.kind) {
            reviewRefs.innerHTML = compareRefsHtml(reviewMeta);
        } else {
            const r = currentReview;
            const parts = [CompareModel.kindLabel(r.kind)];
            if (r.kind === 'worktree') parts.push('against ' + esc(r.base || 'HEAD'));
            else parts.push(esc(CompareModel.refLabel(r.base)) + ' .. ' + esc(CompareModel.refLabel(r.head)));
            if (r.mergebase) parts.push('merge base');
            reviewRefs.innerHTML = parts.join(' · ');
        }
        const movable = currentReview.kind === 'branch' || currentReview.kind === 'range';
        if (movable && reviewNewCommits > 0) {
            reviewNewCommitsEl.textContent = CompareModel.commitsText(reviewNewCommits).replace(/ commit/, ' new commit') + ' since you last looked';
            reviewNewCommitsEl.style.display = '';
        } else {
            reviewNewCommitsEl.style.display = 'none';
        }
        if (error) {
            reviewError.textContent = error;
            reviewError.style.display = '';
        } else {
            reviewError.style.display = 'none';
        }
    }

    // Viewed state onto the files panel and the diff sections: ticks,
    // collapsed sections, the "changed" markers, the "k of n viewed" label.
    function applyViewedState() {
        const files = (currentReview && currentReview.files) || {};
        for (const item of fileListPanel.querySelectorAll('.file-list-item')) {
            const path = item.dataset.path;
            const viewed = Object.prototype.hasOwnProperty.call(files, path);
            item.classList.toggle('viewed', viewed);
            const input = item.querySelector('.viewed-check input');
            if (input) input.checked = viewed;
            const marker = item.querySelector('.file-changed-marker');
            if (marker) marker.style.display = (!viewed && reviewChanged.has(path)) ? '' : 'none';
        }
        for (const section of diffContent.querySelectorAll('.diff-file-section')) {
            const path = section.dataset.path;
            const viewed = Object.prototype.hasOwnProperty.call(files, path);
            section.classList.toggle('viewed', viewed);
            if (!viewed) section.classList.remove('expanded');
            const input = section.querySelector('.viewed-check input');
            if (input) input.checked = viewed;
        }
        updateFilesLabel();
    }

    // The review behind the current page, created on first use: the first
    // viewed tick (or Save as review) on a plain comparison saves it and
    // swaps the URL in place, so back still returns to the list. Callers
    // that arrive while the creation is pending get the same id.
    async function ensureReview() {
        if (currentTarget && currentTarget.kind === 'review') return currentTarget.id;
        return createReviewOnce();
    }

    async function createReview() {
        if (currentTarget && currentTarget.kind === 'review') return currentTarget.id;
        const cmp = compareTargetOf(currentTarget);
        if (!cmp) return null;
        saveReviewBtn.disabled = true;
        try {
            return await postReview(cmp);
        } finally {
            saveReviewBtn.disabled = false;
        }
    }

    async function postReview(cmp) {
        const body = {
            repo: currentRepo,
            base: cmp.base,
            head: cmp.head,
            mergebase: !!cmp.mergebase,
            worktree: !!cmp.worktree,
        };
        const review = await apiJson('POST', '/api/commits/reviews', body);
        if (!review || review.detail) {
            reviewError.textContent = (review && review.detail) || 'Could not save the review';
            reviewError.style.display = '';
            reviewChrome.style.display = '';
            return null;
        }
        currentReview = review;
        reviewChanged = new Set();
        reviewNewCommits = 0;
        currentTarget = { kind: 'review', id: review.id };
        const url = currentView === 'file'
            ? pageUrl('file', currentTarget, currentFilePath)
            : pageUrl('diff', currentTarget);
        history.replaceState(null, '', url);
        if (currentView === 'diff') {
            diffMeta.innerHTML = `<div class="commit-meta-info">Review · ${esc(CompareModel.kindLabel(review.kind))}</div>`;
            renderReviewChrome(null);
            applyViewedState();
            renderAllThreads();
        }
        startReviewPoll();
        return review.id;
    }

    async function toggleViewed(path, viewed) {
        const id = await ensureReview();
        if (!id) {
            applyViewedState();
            return;
        }
        const url = '/api/commits/reviews/' + id + '/viewed/' + encodeURIComponent(path).replace(/%2F/g, '/');
        const review = await enqueueMutation(() => apiJson('PUT', url, { viewed: viewed }));
        if (!review || review.detail) {
            reviewError.textContent = (review && review.detail) || 'Could not update the review';
            reviewError.style.display = '';
            applyViewedState();
            return;
        }
        if (!currentTarget || currentTarget.kind !== 'review' || currentTarget.id !== id) return;
        currentReview = review;
        reviewChanged.delete(path);
        applyViewedState();
    }

    async function saveReviewTitle() {
        if (!currentTarget || currentTarget.kind !== 'review') return;
        const title = reviewTitle.value.trim();
        if (!title) {
            reviewTitle.value = currentReview ? currentReview.title : '';
            return;
        }
        const id = currentTarget.id;
        const review = await enqueueMutation(() => apiJson('PATCH', '/api/commits/reviews/' + id, { title: title }));
        if (review && !review.detail) {
            currentReview = review;
            renderReviewChrome(null);
        }
    }

    async function toggleReviewStatus() {
        if (!currentTarget || currentTarget.kind !== 'review' || !currentReview) return;
        const status = currentReview.status === 'closed' ? 'open' : 'closed';
        const id = currentTarget.id;
        const review = await enqueueMutation(() => apiJson('PATCH', '/api/commits/reviews/' + id, { status: status }));
        if (review && !review.detail) {
            currentReview = review;
            renderReviewChrome(null);
        }
    }

    async function copyForAgent() {
        if (!currentTarget || currentTarget.kind !== 'review') return;
        const text = 'merlin review show ' + currentTarget.id;
        let ok = false;
        try {
            await navigator.clipboard.writeText(text);
            ok = true;
        } catch (_) {}
        if (!ok) {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try { ok = document.execCommand('copy'); } catch (_) {}
            ta.remove();
        }
        const label = reviewCopyBtn.querySelector('span');
        label.textContent = ok ? 'Copied' : text;
        reviewCopyBtn.classList.toggle('done', ok);
        setTimeout(() => {
            label.textContent = 'Copy for agent';
            reviewCopyBtn.classList.remove('done');
        }, 1500);
    }

    // The poll: every 5 seconds while the page is visible, with the known
    // updated stamp. A newer record re-renders the chrome, not the diff, so
    // a change made elsewhere (the agent's CLI) shows up on the phone.
    function startReviewPoll() {
        stopReviewPoll();
        reviewPollTimer = setInterval(pollReview, REVIEW_POLL_MS);
    }

    function stopReviewPoll() {
        if (reviewPollTimer) clearInterval(reviewPollTimer);
        reviewPollTimer = null;
    }

    async function pollReview() {
        if (document.hidden || (currentView !== 'diff' && currentView !== 'file')) return;
        if (!currentTarget || currentTarget.kind !== 'review' || !currentReview) return;
        const id = currentTarget.id;
        let data = null;
        try {
            data = await API.get('/api/commits/reviews/' + id + '?since=' + encodeURIComponent(currentReview.updated || ''));
        } catch (_) {}
        if (!data || !data.changed || !data.review) return;
        if (!currentTarget || currentTarget.kind !== 'review' || currentTarget.id !== id) return;
        currentReview = data.review;
        if (currentView === 'diff') {
            renderReviewChrome(null);
            applyViewedState();
        }
        renderAllThreads();
    }

    // ---------------------------------------------------------------------------
    // Comments: threads on a line or on the review (decisions 11 to 13)
    // ---------------------------------------------------------------------------

    // Tapping a number cell shows the affordance on that row, and the
    // affordance opens the composer under it. Desktop shows it on hover.
    function armCommentCells(tr, cells, path) {
        for (const cell of cells) {
            cell.classList.add('commentable');
            cell.addEventListener('click', (e) => {
                e.stopPropagation();
                if (e.target.closest('.comment-add-btn')) return;
                const armed = tr.classList.contains('comment-armed');
                for (const other of tr.parentElement.querySelectorAll('tr.comment-armed')) {
                    other.classList.remove('comment-armed');
                }
                if (!armed) tr.classList.add('comment-armed');
            });
        }
        const btn = document.createElement('button');
        btn.className = 'comment-add-btn';
        btn.type = 'button';
        btn.title = 'Comment on this line';
        btn.setAttribute('aria-label', 'Comment on line ' + tr.dataset.line);
        btn.textContent = '+';
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            openComposer(tr, path);
        });
        cells[cells.length - 1].appendChild(btn);
    }

    function closeComposers() {
        for (const row of document.querySelectorAll('.comment-composer-row')) row.remove();
    }

    // The composer under a line: a textarea, Comment, Cancel.
    function openComposer(tr, path) {
        closeComposers();
        const side = tr.dataset.side;
        const line = parseInt(tr.dataset.line, 10);
        const row = document.createElement('tr');
        row.className = 'comment-composer-row';
        const td = document.createElement('td');
        td.colSpan = 3;
        td.innerHTML =
            `<div class="thread-composer">` +
                `<textarea class="thread-textarea" rows="3" maxlength="4000" placeholder="Comment on ${esc(path)}:${line}"></textarea>` +
                `<div class="thread-composer-actions">` +
                    `<button type="button" class="thread-btn composer-cancel">Cancel</button>` +
                    `<button type="button" class="thread-btn thread-btn-primary composer-submit">Comment</button>` +
                `</div>` +
            `</div>`;
        row.appendChild(td);
        tr.after(row);
        sizeInlineThreads();
        const textarea = td.querySelector('textarea');
        td.querySelector('.composer-cancel').addEventListener('click', () => row.remove());
        td.querySelector('.composer-submit').addEventListener('click', async () => {
            const ok = await submitComment({ body: textarea.value, path: path, side: side, line: line });
            if (ok) row.remove();
        });
        tr.classList.remove('comment-armed');
        textarea.focus();
    }

    // A new thread. The first comment on a plain comparison creates the
    // review (decision 8), then the comment lands on it.
    async function submitComment(fields) {
        const body = (fields.body || '').trim();
        if (!body) return false;
        const id = await ensureReview();
        if (!id) return false;
        const payload = { body: body };
        if (fields.path) {
            payload.path = fields.path;
            payload.side = fields.side;
            payload.line = fields.line;
        }
        const data = await enqueueMutation(() => apiJson('POST', '/api/commits/reviews/' + id + '/comments', payload));
        if (!data || data.detail || !data.review) {
            reviewError.textContent = (data && data.detail) || 'Could not add the comment';
            reviewError.style.display = '';
            reviewChrome.style.display = '';
            return false;
        }
        if (!currentTarget || currentTarget.kind !== 'review' || currentTarget.id !== id) return true;
        currentReview = data.review;
        if (data.comment && data.comment.id) reviewAnchors[data.comment.id] = 'current';
        renderAllThreads();
        return true;
    }

    async function threadAction(commentId, action, body) {
        if (!currentTarget || currentTarget.kind !== 'review') return;
        const id = currentTarget.id;
        const url = '/api/commits/reviews/' + id + '/comments/' + commentId + '/' + action;
        const review = await enqueueMutation(() => apiJson('POST', url, body || {}));
        if (!review || review.detail) {
            reviewError.textContent = (review && review.detail) || 'Could not update the thread';
            reviewError.style.display = '';
            return;
        }
        if (!currentTarget || currentTarget.kind !== 'review' || currentTarget.id !== id) return;
        currentReview = review;
        renderAllThreads();
    }

    function authorBadge(author) {
        const who = author === 'agent' ? 'agent' : 'you';
        return `<span class="author-badge author-${who}">${who}</span>`;
    }

    function bodyHtml(text) {
        return esc(text).replace(/\n/g, '<br>');
    }

    // The thread component, used inline under a line, at the top of a file
    // for outdated threads, and in the review-wide panel.
    function renderThread(c, opts) {
        opts = opts || {};
        const state = reviewAnchors[c.id] || 'current';
        const resolved = c.status === 'resolved';
        const el = document.createElement('div');
        el.className = 'thread' + (resolved ? ' resolved' : ' open') + (opts.quoted ? ' quoted' : '');
        el.dataset.commentId = c.id;
        const replies = c.replies || [];
        let head =
            `<div class="thread-head">` +
                authorBadge(c.author) +
                `<span class="thread-time">${esc(timeAgo(c.created))}</span>` +
                (c.path && opts.where ? `<span class="thread-where">${esc(c.path)}:${c.line} (${esc(c.side)})</span>` : '') +
                (state === 'moved' ? `<span class="thread-state moved">moved</span>` : '') +
                (state === 'outdated' ? `<span class="thread-state outdated">outdated</span>` : '') +
                (resolved ? `<span class="thread-resolved-label">Resolved · ${replies.length} ${replies.length === 1 ? 'reply' : 'replies'}</span>` +
                            `<button type="button" class="thread-btn thread-expand">Show</button>` : '') +
            `</div>`;
        let quote = '';
        if (opts.quoted && c.line_text != null) {
            quote = `<blockquote class="thread-quote"><span class="thread-quote-line">line ${c.line}</span>${esc(c.line_text)}</blockquote>`;
        }
        let repliesHtml = '';
        for (const r of replies) {
            repliesHtml +=
                `<div class="thread-reply">` +
                    `<div class="thread-head">${authorBadge(r.author)}<span class="thread-time">${esc(timeAgo(r.created))}</span></div>` +
                    `<div class="thread-body">${bodyHtml(r.body)}</div>` +
                `</div>`;
        }
        el.innerHTML =
            head +
            `<div class="thread-content">` +
                quote +
                `<div class="thread-body">${bodyHtml(c.body)}</div>` +
                `<div class="thread-replies">${repliesHtml}</div>` +
                `<div class="thread-actions">` +
                    `<textarea class="thread-textarea thread-reply-text" rows="1" maxlength="4000" placeholder="Reply"></textarea>` +
                    `<button type="button" class="thread-btn thread-reply-btn">Reply</button>` +
                    (resolved
                        ? `<button type="button" class="thread-btn thread-reopen-btn">Reopen</button>`
                        : `<button type="button" class="thread-btn thread-btn-primary thread-resolve-btn">Resolve</button>`) +
                `</div>` +
            `</div>`;
        const expand = el.querySelector('.thread-expand');
        if (expand) {
            expand.addEventListener('click', () => {
                const open = el.classList.toggle('expanded');
                expand.textContent = open ? 'Hide' : 'Show';
            });
        }
        const replyText = el.querySelector('.thread-reply-text');
        el.querySelector('.thread-reply-btn').addEventListener('click', () => {
            const body = replyText.value.trim();
            if (!body) return;
            threadAction(c.id, 'replies', { body: body });
        });
        const resolveBtn = el.querySelector('.thread-resolve-btn');
        if (resolveBtn) {
            resolveBtn.addEventListener('click', () => {
                const body = replyText.value.trim();
                threadAction(c.id, 'resolve', body ? { message: body } : {});
            });
        }
        const reopenBtn = el.querySelector('.thread-reopen-btn');
        if (reopenBtn) reopenBtn.addEventListener('click', () => threadAction(c.id, 'reopen', {}));
        return el;
    }

    function threadRow(c, opts, colSpan) {
        const row = document.createElement('tr');
        row.className = 'comment-thread-row';
        const td = document.createElement('td');
        td.colSpan = colSpan;
        td.appendChild(renderThread(c, opts));
        row.appendChild(td);
        return row;
    }

    // A thread or composer inside a scrolling table gets the width of the
    // table's visible wrapper, so a long sentence never widens the table
    // and the thread stays in view while the code scrolls sideways.
    function sizeInlineThreads() {
        for (const el of document.querySelectorAll('.comment-thread-row .thread, .comment-composer-row .thread-composer')) {
            const wrapper = el.closest('.diff-table-scroll, .file-content');
            if (wrapper) el.style.width = wrapper.clientWidth + 'px';
        }
    }

    // Place every thread of the record: under its line in the diff (or the
    // full file), at the top of its file when outdated or not in the hunks
    // shown, and in the review-wide panel. Rebuilt on every change.
    function renderAllThreads() {
        placeThreads();
        sizeInlineThreads();
    }

    function placeThreads() {
        for (const row of document.querySelectorAll('.comment-thread-row')) row.remove();
        for (const top of document.querySelectorAll('.file-threads-top-inline')) top.remove();
        reviewThreads.innerHTML = '';
        fileThreadsTop.innerHTML = '';
        fileThreadsTop.style.display = 'none';
        const comments = (currentReview && currentReview.comments) || [];
        const openByPath = {};
        for (const c of comments) {
            if (c.path && c.status === 'open') openByPath[c.path] = (openByPath[c.path] || 0) + 1;
        }
        for (const section of diffContent.querySelectorAll('.diff-file-section')) {
            const count = section.querySelector('.diff-file-count');
            const n = openByPath[section.dataset.path] || 0;
            if (count) count.textContent = n ? n + ' open' : '';
        }
        for (const c of comments) {
            if (!c.path) {
                reviewThreads.appendChild(renderThread(c, {}));
                continue;
            }
            if (currentView === 'file') {
                if (c.path !== currentFilePath) continue;
                const state = reviewAnchors[c.id] || 'current';
                const row = state !== 'outdated' && c.side === 'new'
                    ? fileContent.querySelector('#file-line-' + c.line)
                    : null;
                if (row) {
                    let after = row;
                    while (after.nextElementSibling && after.nextElementSibling.classList.contains('comment-thread-row')) after = after.nextElementSibling;
                    after.after(threadRow(c, {}, 3));
                } else {
                    fileThreadsTop.appendChild(renderThread(c, { quoted: true, where: true }));
                    fileThreadsTop.style.display = '';
                }
                continue;
            }
            const section = document.getElementById('diff-file-' + c.path);
            if (!section) continue;
            const state = reviewAnchors[c.id] || 'current';
            const row = state !== 'outdated'
                ? section.querySelector(`tr[data-side="${c.side}"][data-line="${c.line}"]`)
                : null;
            if (row) {
                let after = row;
                while (after.nextElementSibling && after.nextElementSibling.classList.contains('comment-thread-row')) after = after.nextElementSibling;
                after.after(threadRow(c, {}, 3));
            } else {
                let top = section.querySelector('.file-threads-top-inline');
                if (!top) {
                    top = document.createElement('div');
                    top.className = 'file-threads-top file-threads-top-inline';
                    section.querySelector('.diff-file-header').after(top);
                }
                top.appendChild(renderThread(c, { quoted: true, where: true }));
            }
        }
    }

    function toggleFileList() {
        const panel = fileListPanel;
        const btn = fileListToggleBtn;
        if (panel.style.display === 'none') {
            panel.style.display = '';
            btn.classList.add('open');
        } else {
            panel.style.display = 'none';
            btn.classList.remove('open');
        }
    }

    // ---------------------------------------------------------------------------
    // Full File with Gutters (View 3)
    // ---------------------------------------------------------------------------

    async function loadFile(target, filePath) {
        fileContent.innerHTML = '';
        fileLoading.style.display = '';
        gutterFab.style.display = 'none';
        gutterLines = [];
        gutterIndex = -1;
        lineHunkMap = [];

        fileMeta.innerHTML = `<div class="file-meta-path">${esc(filePath)}</div>`;
        fileThreadsTop.innerHTML = '';
        fileThreadsTop.style.display = 'none';

        if (target.kind === 'review' && !(currentReview && currentReview.id === target.id)) {
            // A deep link into a review's file: the full load, so the threads
            // of this file come with their anchor states (moved, outdated).
            const rv = await API.get('/api/commits/reviews/' + target.id);
            if (rv && rv.review) {
                currentReview = rv.review;
                reviewMeta = rv.comparison;
                reviewChanged = new Set(rv.changed_since_viewed || []);
                reviewNewCommits = rv.new_commits || 0;
                reviewAnchors = rv.anchors || {};
            }
        }

        const data = await API.get(apiUrl('file', target, filePath));
        fileLoading.style.display = 'none';

        if (!data) return;
        if (data.detail) {
            fileContent.innerHTML = `<div class="empty-state"><p>${esc(data.detail)}</p></div>`;
            return;
        }

        // Pre-process: collect deleted_lines per hunk.
        let hunkId = 0;
        const hunkMap = [];
        for (let i = 0; i < data.lines.length; i++) {
            const line = data.lines[i];
            if (line.gutter === 'modified' || line.gutter === 'deleted') {
                let deletedLines = [];
                if (line.deleted_lines && line.deleted_lines.length > 0) {
                    deletedLines = line.deleted_lines;
                }
                const currentHunk = hunkId++;
                hunkMap[i] = { id: currentHunk, deletedLines: deletedLines, isLast: false };
                lineHunkMap[i] = currentHunk;
                let last = i;
                for (let j = i + 1; j < data.lines.length; j++) {
                    const next = data.lines[j];
                    if (next.gutter === 'modified' || next.gutter === 'deleted') {
                        if (next.deleted_lines && next.deleted_lines.length > 0) {
                            deletedLines = deletedLines.concat(next.deleted_lines);
                        }
                        hunkMap[j] = { id: currentHunk, deletedLines: null, isLast: false };
                        lineHunkMap[j] = currentHunk;
                        last = j;
                    } else {
                        break;
                    }
                }
                hunkMap[i].deletedLines = deletedLines;
                hunkMap[last].isLast = true;
                i = last;
            }
        }

        // Reset diff mode state
        fileContent.classList.remove('diff-mode');
        diffToggle.classList.remove('active');

        const table = document.createElement('table');
        table.className = 'file-table';
        const tbody = document.createElement('tbody');

        for (let i = 0; i < data.lines.length; i++) {
            const line = data.lines[i];
            const hunk = hunkMap[i];

            if (hunk && hunk.deletedLines && hunk.deletedLines.length > 0) {
                for (const dl of hunk.deletedLines) {
                    const delTr = document.createElement('tr');
                    delTr.className = 'file-diff-del-row';

                    const delGutter = document.createElement('td');
                    delGutter.className = 'file-gutter file-gutter-deleted';

                    const delLineNo = document.createElement('td');
                    delLineNo.className = 'file-line-no';

                    const delContent = document.createElement('td');
                    delContent.className = 'file-line-content';
                    const delCode = document.createElement('code');
                    delCode.textContent = dl;
                    delContent.appendChild(delCode);

                    delTr.appendChild(delGutter);
                    delTr.appendChild(delLineNo);
                    delTr.appendChild(delContent);
                    tbody.appendChild(delTr);
                }
            }

            const tr = document.createElement('tr');
            tr.id = 'file-line-' + line.no;

            const gutter = document.createElement('td');
            gutter.className = 'file-gutter';
            if (line.gutter) {
                gutter.classList.add('file-gutter-' + line.gutter);
                tr.classList.add('file-line-' + line.gutter);
                gutterLines.push(i);
            }

            const lineNo = document.createElement('td');
            lineNo.className = 'file-line-no';
            lineNo.textContent = line.no;

            const content = document.createElement('td');
            content.className = 'file-line-content';
            const code = document.createElement('code');
            code.textContent = line.content;
            content.appendChild(code);

            tr.appendChild(gutter);
            tr.appendChild(lineNo);
            tr.appendChild(content);
            tr.dataset.side = 'new';
            tr.dataset.line = line.no;
            armCommentCells(tr, [lineNo], filePath);
            tbody.appendChild(tr);
        }

        table.appendChild(tbody);
        fileContent.appendChild(table);

        applySyntaxHighlighting(filePath);
        renderAllThreads();
        if (target.kind === 'review') startReviewPoll();

        if (gutterLines.length > 0) {
            const hunks = [gutterLines[0]];
            for (let i = 1; i < gutterLines.length; i++) {
                if (gutterLines[i] - gutterLines[i - 1] > 1) {
                    hunks.push(gutterLines[i]);
                }
            }
            gutterLines = hunks;
            gutterFab.style.display = '';
            updateFabCounter();

            if (targetLine != null) {
                fileContent.classList.add('diff-mode');
                diffToggle.classList.add('active');
                let bestIdx = 0;
                let bestDist = Infinity;
                for (let h = 0; h < gutterLines.length; h++) {
                    const lineNo = data.lines[gutterLines[h]]?.no || 0;
                    const dist = Math.abs(lineNo - targetLine);
                    if (dist < bestDist) { bestDist = dist; bestIdx = h; }
                }
                gutterIndex = bestIdx;
                scrollToGutter(gutterIndex);
                targetLine = null;
            }
        }
    }

    function applySyntaxHighlighting(filePath) {
        if (typeof hljs === 'undefined') return;
        const ext = filePath.split('.').pop();
        const table = fileContent.querySelector('.file-table');
        if (!table) return;

        const codeElements = table.querySelectorAll('tr:not(.file-diff-del-row) .file-line-content code');
        const allText = Array.from(codeElements).map(c => c.textContent).join('\n');

        let result;
        try {
            const lang = hljs.getLanguage(ext) ? ext : undefined;
            result = lang ? hljs.highlight(allText, { language: lang }) : hljs.highlightAuto(allText);
        } catch {
            return;
        }

        const tmp = document.createElement('div');
        tmp.innerHTML = result.value;
        const highlightedLines = tmp.innerHTML.split('\n');

        codeElements.forEach((code, i) => {
            if (highlightedLines[i] !== undefined) {
                code.innerHTML = highlightedLines[i];
            }
        });
    }

    function toggleDiffMode() {
        fileContent.classList.toggle('diff-mode');
        diffToggle.classList.toggle('active');
    }

    function toggleWrap() {
        fileContent.classList.toggle('wrapped');
        wrapToggle.classList.toggle('active');
    }

    // ---------------------------------------------------------------------------
    // Gutter FAB navigation
    // ---------------------------------------------------------------------------

    function jumpToNextGutter() {
        if (gutterLines.length === 0) return;
        gutterIndex = (gutterIndex + 1) % gutterLines.length;
        scrollToGutter(gutterIndex);
    }

    function jumpToPrevGutter() {
        if (gutterLines.length === 0) return;
        gutterIndex = gutterIndex <= 0 ? gutterLines.length - 1 : gutterIndex - 1;
        scrollToGutter(gutterIndex);
    }

    function scrollToGutter(idx) {
        const lineIdx = gutterLines[idx];
        const table = fileContent.querySelector('.file-table');
        if (!table) return;

        const prev = table.querySelector('.gutter-active');
        if (prev) prev.classList.remove('gutter-active');

        const rows = table.querySelectorAll('tbody > tr:not(.file-diff-del-row)');
        if (rows[lineIdx]) {
            rows[lineIdx].classList.add('gutter-active');
            rows[lineIdx].scrollIntoView({ behavior: 'smooth', block: 'center' });
        }

        updateFabCounter();
    }

    function updateFabCounter() {
        fabCounter.textContent = (gutterIndex + 1) + '/' + gutterLines.length;
    }

    // ---------------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------------

    function esc(str) {
        if (str == null) return '';
        const d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }

})();
