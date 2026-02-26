/* File browser — SPA with directory listing + file viewer */

(function() {
    'use strict';

    // State
    let currentView = 'dir';   // 'dir' or 'file'
    let currentPath = '/';     // current filesystem path

    // DOM refs
    let dirView, fileView;
    let breadcrumbs, dirEntries, dirEmpty, dirError, dirErrorMsg, dirLoading;
    let fileMeta, fileContent, fileLoading, wrapToggle, downloadLink;

    // ---------------------------------------------------------------------------
    // Init
    // ---------------------------------------------------------------------------

    document.addEventListener('DOMContentLoaded', () => {
        dirView = document.getElementById('dir-view');
        fileView = document.getElementById('file-view');
        breadcrumbs = document.getElementById('breadcrumbs');
        dirEntries = document.getElementById('dir-entries');
        dirEmpty = document.getElementById('dir-empty');
        dirError = document.getElementById('dir-error');
        dirErrorMsg = document.getElementById('dir-error-msg');
        dirLoading = document.getElementById('dir-loading');
        fileMeta = document.getElementById('file-meta');
        fileContent = document.getElementById('file-content');
        fileLoading = document.getElementById('file-loading');
        wrapToggle = document.getElementById('wrap-toggle');
        downloadLink = document.getElementById('download-link');

        document.getElementById('file-back-btn').addEventListener('click', goToParent);
        wrapToggle.addEventListener('click', toggleWrap);

        window.addEventListener('popstate', routeFromUrl);
        routeFromUrl();
    });

    // ---------------------------------------------------------------------------
    // Routing
    // ---------------------------------------------------------------------------

    function routeFromUrl() {
        const urlPath = window.location.pathname;
        // /files or /files/ => root
        // /files/home/user/... => /home/user/...
        let fsPath = '/';
        if (urlPath.startsWith('/files/')) {
            fsPath = '/' + urlPath.slice(7);
        } else if (urlPath === '/files') {
            fsPath = '/';
        }
        browse(fsPath, false);
    }

    function navigateTo(fsPath, pushState) {
        const urlPath = fsPath === '/' ? '/files' : '/files' + fsPath;
        if (pushState && window.location.pathname !== urlPath) {
            history.pushState(null, '', urlPath);
        }
        browse(fsPath, false);
    }

    function goToParent() {
        // Navigate to parent directory of current path
        const parent = currentPath.replace(/\/[^/]*\/?$/, '') || '/';
        navigateTo(parent, true);
    }

    // ---------------------------------------------------------------------------
    // Browse (single entry point — handles both dirs and files)
    // ---------------------------------------------------------------------------

    async function browse(fsPath, pushState) {
        currentPath = fsPath;

        // Show loading in directory view initially
        showDirView();
        dirEntries.innerHTML = '';
        dirEmpty.style.display = 'none';
        dirError.style.display = 'none';
        dirLoading.style.display = '';

        let resp, data;
        try {
            resp = await fetch('/api/files/browse?path=' + encodeURIComponent(fsPath));
            if (resp.status === 401) {
                window.location.reload();
                return;
            }
            data = await resp.json();
        } catch {
            dirLoading.style.display = 'none';
            showError('Failed to load path');
            return;
        }

        dirLoading.style.display = 'none';

        if (!resp.ok) {
            showError(data.detail || 'Error ' + resp.status);
            return;
        }

        if (data.type === 'directory') {
            showDirView();
            renderBreadcrumbs(fsPath);
            renderDirectory(data);
        } else if (data.type === 'file') {
            showFileView();
            renderFileViewer(data);
        }
    }

    function showError(msg) {
        showDirView();
        renderBreadcrumbs(currentPath);
        dirError.style.display = '';
        dirErrorMsg.textContent = msg;
    }

    // ---------------------------------------------------------------------------
    // View switching
    // ---------------------------------------------------------------------------

    function showDirView() {
        currentView = 'dir';
        dirView.style.display = '';
        fileView.style.display = 'none';
    }

    function showFileView() {
        currentView = 'file';
        dirView.style.display = 'none';
        fileView.style.display = '';
    }

    // ---------------------------------------------------------------------------
    // Breadcrumbs
    // ---------------------------------------------------------------------------

    function renderBreadcrumbs(fsPath) {
        breadcrumbs.innerHTML = '';

        // Root link
        const rootLink = document.createElement('a');
        rootLink.className = 'breadcrumb-segment';
        rootLink.textContent = '/';
        rootLink.href = '/files';
        rootLink.addEventListener('click', (e) => {
            e.preventDefault();
            navigateTo('/', true);
        });
        breadcrumbs.appendChild(rootLink);

        if (fsPath === '/') return;

        const parts = fsPath.split('/').filter(Boolean);
        let cumulative = '';

        for (let i = 0; i < parts.length; i++) {
            cumulative += '/' + parts[i];

            const sep = document.createElement('span');
            sep.className = 'breadcrumb-sep';
            sep.textContent = '/';
            breadcrumbs.appendChild(sep);

            const link = document.createElement('a');
            link.className = 'breadcrumb-segment';
            link.textContent = parts[i];
            const targetPath = cumulative;
            link.href = '/files' + targetPath;
            link.addEventListener('click', (e) => {
                e.preventDefault();
                navigateTo(targetPath, true);
            });

            if (i === parts.length - 1) {
                link.classList.add('breadcrumb-current');
            }

            breadcrumbs.appendChild(link);
        }
    }

    // ---------------------------------------------------------------------------
    // Directory listing
    // ---------------------------------------------------------------------------

    function renderDirectory(data) {
        dirEntries.innerHTML = '';

        if (data.entries.length === 0) {
            dirEmpty.style.display = '';
            return;
        }

        for (const entry of data.entries) {
            const row = document.createElement('div');
            row.className = 'dir-entry';
            if (entry.is_hidden) row.classList.add('dir-entry-hidden');

            // Icon
            const icon = document.createElement('span');
            icon.className = 'dir-entry-icon';
            if (entry.type === 'dir') {
                icon.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
                icon.classList.add('icon-folder');
            } else if (isImageName(entry.name)) {
                icon.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>';
                icon.classList.add('icon-image');
            } else {
                icon.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>';
                icon.classList.add('icon-file');
            }
            row.appendChild(icon);

            // Name
            const name = document.createElement('span');
            name.className = 'dir-entry-name';
            name.textContent = entry.name;
            if (entry.type === 'dir') name.textContent += '/';
            row.appendChild(name);

            // Size (files only)
            const size = document.createElement('span');
            size.className = 'dir-entry-size';
            if (entry.type === 'file' && entry.size != null) {
                size.textContent = formatSize(entry.size);
            }
            row.appendChild(size);

            // Time
            const time = document.createElement('span');
            time.className = 'dir-entry-time';
            if (entry.mtime != null) {
                time.textContent = timeAgo(new Date(entry.mtime * 1000).toISOString());
                time.title = new Date(entry.mtime * 1000).toLocaleString();
            }
            row.appendChild(time);

            // Click handler
            const entryPath = currentPath === '/'
                ? '/' + entry.name
                : currentPath + '/' + entry.name;

            row.addEventListener('click', () => navigateTo(entryPath, true));
            dirEntries.appendChild(row);
        }
    }

    // ---------------------------------------------------------------------------
    // File viewer
    // ---------------------------------------------------------------------------

    async function renderFileViewer(info) {
        fileContent.innerHTML = '';
        fileContent.classList.remove('wrapped');
        wrapToggle.classList.remove('active');
        wrapToggle.style.display = 'none';
        fileLoading.style.display = 'none';

        // Meta
        fileMeta.innerHTML = '<div class="file-meta-path">' + esc(info.name) + '</div>';

        // Download link
        downloadLink.href = '/api/files/raw?path=' + encodeURIComponent(info.path);
        downloadLink.download = info.name;
        downloadLink.style.display = '';

        if (info.is_image) {
            renderImagePreview(info);
        } else if (info.is_text) {
            wrapToggle.style.display = '';
            await renderTextFile(info);
        } else {
            renderBinaryInfo(info);
        }
    }

    // ---------------------------------------------------------------------------
    // Text file rendering
    // ---------------------------------------------------------------------------

    async function renderTextFile(info) {
        fileLoading.style.display = '';

        const data = await API.get('/api/files/content?path=' + encodeURIComponent(info.path));
        fileLoading.style.display = 'none';

        if (!data) {
            fileContent.innerHTML = '<div class="file-error">Failed to load file content</div>';
            return;
        }

        const lines = data.content.split('\n');
        // Remove trailing empty line from split
        if (lines.length > 0 && lines[lines.length - 1] === '') {
            lines.pop();
        }

        const table = document.createElement('table');
        table.className = 'file-table';
        const tbody = document.createElement('tbody');

        for (let i = 0; i < lines.length; i++) {
            const tr = document.createElement('tr');

            const lineNo = document.createElement('td');
            lineNo.className = 'file-line-no';
            lineNo.textContent = i + 1;

            const content = document.createElement('td');
            content.className = 'file-line-content';
            const code = document.createElement('code');
            code.textContent = lines[i];
            content.appendChild(code);

            tr.appendChild(lineNo);
            tr.appendChild(content);
            tbody.appendChild(tr);
        }

        table.appendChild(tbody);
        fileContent.appendChild(table);

        // Truncation notice
        if (data.truncated) {
            const notice = document.createElement('div');
            notice.className = 'truncation-notice';
            notice.innerHTML = 'File truncated at 2 MB. <a href="/api/files/raw?path=' +
                encodeURIComponent(info.path) + '">Download full file</a>';
            fileContent.appendChild(notice);
        }

        // Syntax highlighting
        applySyntaxHighlighting(info.name);
    }

    function applySyntaxHighlighting(filename) {
        if (typeof hljs === 'undefined') return;

        const ext = filename.includes('.') ? filename.split('.').pop() : '';
        const table = fileContent.querySelector('.file-table');
        if (!table) return;

        const codeElements = table.querySelectorAll('.file-line-content code');
        const allText = Array.from(codeElements).map(c => c.textContent).join('\n');

        let result;
        try {
            const lang = ext && hljs.getLanguage(ext) ? ext : undefined;
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

    // ---------------------------------------------------------------------------
    // Image preview
    // ---------------------------------------------------------------------------

    function renderImagePreview(info) {
        const wrapper = document.createElement('div');
        wrapper.className = 'image-preview';

        const img = document.createElement('img');
        img.src = '/api/files/raw?path=' + encodeURIComponent(info.path);
        img.alt = info.name;
        img.loading = 'lazy';
        wrapper.appendChild(img);

        const meta = document.createElement('div');
        meta.className = 'image-meta';
        meta.textContent = formatSize(info.size) + ' · ' + info.mime_type;
        wrapper.appendChild(meta);

        fileContent.appendChild(wrapper);
    }

    // ---------------------------------------------------------------------------
    // Binary file info
    // ---------------------------------------------------------------------------

    function renderBinaryInfo(info) {
        const wrapper = document.createElement('div');
        wrapper.className = 'binary-info';

        const icon = document.createElement('div');
        icon.className = 'binary-icon';
        icon.innerHTML = '<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>';
        wrapper.appendChild(icon);

        const name = document.createElement('div');
        name.className = 'binary-name';
        name.textContent = info.name;
        wrapper.appendChild(name);

        const details = document.createElement('div');
        details.className = 'binary-details';
        const ext = info.name.includes('.') ? info.name.split('.').pop() : '';
        details.textContent = formatSize(info.size) + (ext ? ' · .' + ext : '');
        wrapper.appendChild(details);

        const dlBtn = document.createElement('a');
        dlBtn.className = 'binary-download-btn';
        dlBtn.href = '/api/files/raw?path=' + encodeURIComponent(info.path);
        dlBtn.download = info.name;
        dlBtn.textContent = 'Download';
        wrapper.appendChild(dlBtn);

        fileContent.appendChild(wrapper);
    }

    // ---------------------------------------------------------------------------
    // Wrap toggle
    // ---------------------------------------------------------------------------

    function toggleWrap() {
        fileContent.classList.toggle('wrapped');
        wrapToggle.classList.toggle('active');
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

    function formatSize(bytes) {
        if (bytes == null) return '';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
    }

    const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp', 'ico']);

    function isImageName(name) {
        const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
        return IMAGE_EXTS.has(ext);
    }

})();
