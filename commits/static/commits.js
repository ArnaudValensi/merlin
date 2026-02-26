/* Commit browser — view management, diff rendering, gutter logic */

(function() {
    'use strict';

    // State
    let currentView = 'list';
    let commits = [];
    let loadedCount = 0;
    const PAGE_SIZE = 50;
    let searchTimeout = null;
    let currentHash = null;
    let currentFilePath = null;
    let gutterLines = [];   // indices into file lines that have gutters
    let gutterIndex = -1;
    let lineHunkMap = [];   // per-line-index: hunk id or null
    let targetLine = null;  // line number to scroll to after file load

    // DOM refs (set in init)
    let listView, diffView, fileView;
    let commitList, listEmpty, loadMoreBtn, listLoading;
    let searchInput, sinceInput, untilInput;
    let diffMeta, diffContent, diffLoading, fileListToggle, fileListPanel, fileListCount, fileListToggleBtn;
    let fileMeta, fileContent, fileLoading, diffToggle, wrapToggle, gutterFab, fabCounter;

    // ---------------------------------------------------------------------------
    // Init
    // ---------------------------------------------------------------------------

    document.addEventListener('DOMContentLoaded', () => {
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

        // Event listeners
        searchInput.addEventListener('input', debounceSearch);
        sinceInput.addEventListener('change', resetAndLoad);
        untilInput.addEventListener('change', resetAndLoad);
        loadMoreBtn.addEventListener('click', loadMore);
        document.getElementById('diff-back-btn').addEventListener('click', () => navigateTo('list'));
        document.getElementById('file-back-btn').addEventListener('click', () => {
            if (currentHash) navigateTo('diff', currentHash);
            else navigateTo('list');
        });
        fileListToggleBtn.addEventListener('click', toggleFileList);
        diffToggle.addEventListener('click', toggleDiffMode);
        wrapToggle.addEventListener('click', toggleWrap);
        document.getElementById('fab-next').addEventListener('click', jumpToNextGutter);
        document.getElementById('fab-prev').addEventListener('click', jumpToPrevGutter);

        // Handle browser back/forward
        window.addEventListener('popstate', handlePopState);

        // Route from URL
        routeFromUrl();
    });

    // ---------------------------------------------------------------------------
    // Routing
    // ---------------------------------------------------------------------------

    function routeFromUrl() {
        const path = window.location.pathname;
        const m_file = path.match(/^\/commits\/([0-9a-f]+)\/file\/(.+)$/);
        const m_diff = path.match(/^\/commits\/([0-9a-f]+)$/);

        if (m_file) {
            showDiffView(m_file[1], false);
            showFileView(m_file[1], m_file[2], false);
        } else if (m_diff) {
            showDiffView(m_diff[1], false);
        } else {
            showListView(false);
        }
    }

    function handlePopState() {
        routeFromUrl();
    }

    function navigateTo(view, hash, filePath) {
        let url;
        if (view === 'list') {
            url = '/commits';
            showListView(true);
        } else if (view === 'diff') {
            url = '/commits/' + hash;
            showDiffView(hash, true);
        } else if (view === 'file') {
            url = '/commits/' + hash + '/file/' + filePath;
            showFileView(hash, filePath, true);
        }
    }

    function pushUrl(url) {
        if (window.location.pathname !== url) {
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
        if (pushState) pushUrl('/commits');
        if (commits.length === 0) {
            loadCommits();
        }
    }

    function showDiffView(hash, pushState) {
        currentView = 'diff';
        currentHash = hash;
        listView.style.display = 'none';
        diffView.style.display = '';
        fileView.style.display = 'none';
        if (pushState) pushUrl('/commits/' + hash);
        loadDiff(hash);
    }

    function showFileView(hash, filePath, pushState) {
        currentView = 'file';
        currentHash = hash;
        currentFilePath = filePath;
        listView.style.display = 'none';
        diffView.style.display = 'none';
        fileView.style.display = '';
        if (pushState) pushUrl('/commits/' + hash + '/file/' + filePath);
        loadFile(hash, filePath);
    }

    // ---------------------------------------------------------------------------
    // Commit List (View 1)
    // ---------------------------------------------------------------------------

    async function loadCommits(append) {
        if (!append) {
            loadedCount = 0;
            commits = [];
            commitList.innerHTML = '';
        }

        listLoading.style.display = '';
        loadMoreBtn.style.display = 'none';
        listEmpty.style.display = 'none';

        const params = new URLSearchParams();
        params.set('skip', loadedCount);
        params.set('limit', PAGE_SIZE);
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

        if (data.length >= PAGE_SIZE) {
            loadMoreBtn.style.display = '';
        }
    }

    function renderCommitItem(c) {
        const el = document.createElement('div');
        el.className = 'commit-item';
        el.addEventListener('click', () => navigateTo('diff', c.hash));

        const statsHtml = (c.insertions || c.deletions)
            ? `<div class="commit-stats">` +
              (c.insertions ? `<span class="stat-add">+${c.insertions}</span>` : '') +
              (c.deletions ? `<span class="stat-del">-${c.deletions}</span>` : '') +
              `</div>`
            : '';

        el.innerHTML =
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
    // Commit Diff (View 2)
    // ---------------------------------------------------------------------------

    async function loadDiff(hash) {
        diffContent.innerHTML = '';
        diffLoading.style.display = '';
        fileListToggle.style.display = 'none';

        // Load metadata and diff in parallel
        const [meta, diff] = await Promise.all([
            API.get('/api/commits/' + hash),
            API.get('/api/commits/' + hash + '/diff'),
        ]);

        diffLoading.style.display = 'none';
        if (!meta || !diff) return;

        // Render header
        diffMeta.innerHTML =
            `<div class="commit-meta-message">${esc(meta.message)}</div>` +
            `<div class="commit-meta-info">` +
                `<span class="commit-meta-hash">${esc(meta.short)}</span> · ` +
                `${esc(meta.author)} · ${timeAgo(meta.date)}` +
            `</div>`;

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
        }

        // Render diff sections
        for (const file of diff.files) {
            diffContent.appendChild(renderDiffFile(file, hash));
        }
    }

    function renderDiffFile(file, hash) {
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
                navigateTo('file', hash, file.path);
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
                        navigateTo('file', hash, file.path);
                    });
                }
                hdr.innerHTML = `<td colspan="3">${esc(hunk.header)}</td>`;
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

    async function loadFile(hash, filePath) {
        fileContent.innerHTML = '';
        fileLoading.style.display = '';
        gutterFab.style.display = 'none';
        gutterLines = [];
        gutterIndex = -1;
        lineHunkMap = [];

        fileMeta.innerHTML = `<div class="file-meta-path">${esc(filePath)}</div>`;

        const data = await API.get('/api/commits/' + hash + '/file/' + encodeURIComponent(filePath));
        fileLoading.style.display = 'none';

        if (!data) return;

        // Pre-process: collect deleted_lines per hunk.
        // Backend puts all deleted_lines on the first modified/deleted line of a group.
        // We identify each hunk (consecutive modified/deleted lines) and attach a shared
        // hunk ID so all lines in the group toggle the same expansion block.
        let hunkId = 0;
        const hunkMap = [];  // per-line: { id, deletedLines } or null
        for (let i = 0; i < data.lines.length; i++) {
            const line = data.lines[i];
            if (line.gutter === 'modified' || line.gutter === 'deleted') {
                // Collect all deleted_lines from this hunk group
                let deletedLines = [];
                if (line.deleted_lines && line.deleted_lines.length > 0) {
                    deletedLines = line.deleted_lines;
                }
                const currentHunk = hunkId++;
                hunkMap[i] = { id: currentHunk, deletedLines: deletedLines, isLast: false };
                lineHunkMap[i] = currentHunk;
                // Mark consecutive siblings as same hunk
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
                // Store full deleted lines on hunk and mark last line
                hunkMap[i].deletedLines = deletedLines;
                hunkMap[last].isLast = true;
                i = last;  // skip past group
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

            // Before the first line of a hunk, insert deleted lines as proper rows
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

            // Gutter bar
            const gutter = document.createElement('td');
            gutter.className = 'file-gutter';
            if (line.gutter) {
                gutter.classList.add('file-gutter-' + line.gutter);
                tr.classList.add('file-line-' + line.gutter);
                gutterLines.push(i);
            }

            // Line number
            const lineNo = document.createElement('td');
            lineNo.className = 'file-line-no';
            lineNo.textContent = line.no;

            // Content
            const content = document.createElement('td');
            content.className = 'file-line-content';

            // Use highlight.js for syntax highlighting
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

        // Apply syntax highlighting
        applySyntaxHighlighting(filePath);

        // Reduce gutterLines to hunk starts (first line of each consecutive group)
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

            // If navigated from diff view, enable diff mode and scroll to target hunk
            if (targetLine != null) {
                fileContent.classList.add('diff-mode');
                diffToggle.classList.add('active');
                // Find the hunk closest to the target line
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

        // Collect all code content, highlight as a block, then redistribute
        // Exclude deleted diff rows — they're not part of the file content
        const codeElements = table.querySelectorAll('tr:not(.file-diff-del-row) .file-line-content code');
        const allText = Array.from(codeElements).map(c => c.textContent).join('\n');

        let result;
        try {
            const lang = hljs.getLanguage(ext) ? ext : undefined;
            result = lang ? hljs.highlight(allText, { language: lang }) : hljs.highlightAuto(allText);
        } catch {
            return;
        }

        // Parse the highlighted HTML and redistribute per line
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

        // Remove previous highlight
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
