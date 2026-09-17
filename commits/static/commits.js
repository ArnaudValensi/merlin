/* Commit browser: view management, diff rendering, gutter logic.
   Every diff view is a comparison "target": {kind: 'commit', hash} for a
   single commit, or {kind: 'compare', base, head, mergebase, worktree} for
   a base..head comparison (range, branch, working tree). */

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
        const base = target.kind === 'commit' ? '/commits/' + target.hash : '/commits/compare';
        const path = view === 'file' ? base + '/file/' + filePath : base;
        return withQuery(path, targetParams(target));
    }

    function apiUrl(what, target, filePath) {
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

        if (m_cmp_file) {
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
        if (!append) loadWorktreeRow();

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

    async function loadDiff(target) {
        diffContent.innerHTML = '';
        diffLoading.style.display = '';
        fileListToggle.style.display = 'none';
        compareCommits.style.display = 'none';
        diffMeta.innerHTML = '';

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
        if (meta.files && meta.files.length > 0) {
            fileListToggle.style.display = '';
            fileListCount.textContent = meta.files.length;
            fileListPanel.innerHTML = '';
            for (const f of meta.files) {
                const item = document.createElement('div');
                item.className = 'file-list-item';
                item.innerHTML =
                    `<span class="file-status file-status-${esc(f.status)}">${esc(f.status)}</span>` +
                    `<span class="file-list-path">${esc(f.path)}</span>` +
                    `<span class="file-list-stats">` +
                        (f.insertions ? `<span class="stat-add">+${f.insertions}</span>` : '') +
                        (f.deletions ? `<span class="stat-del">-${f.deletions}</span>` : '') +
                    `</span>`;
                const path = f.path;
                item.addEventListener('click', () => {
                    // Scroll to the file section in the diff
                    const section = document.getElementById('diff-file-' + CSS.escape(path));
                    if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
                });
                fileListPanel.appendChild(item);
            }
        } else if (target.kind === 'compare') {
            diffContent.innerHTML = '<div class="empty-state"><p>Nothing to compare: no changes between these points</p></div>';
        }

        // Render diff sections
        for (const file of diff.files) {
            diffContent.appendChild(renderDiffFile(file, target));
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

        // Header
        const header = document.createElement('div');
        header.className = 'diff-file-header';

        const pathSpan = document.createElement('span');
        pathSpan.className = 'diff-file-path';
        pathSpan.textContent = file.path;
        header.appendChild(pathSpan);

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
            tbody.appendChild(tr);
        }

        table.appendChild(tbody);
        fileContent.appendChild(table);

        applySyntaxHighlighting(filePath);

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
