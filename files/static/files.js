/* File browser — SPA with directory listing + file viewer */

(function() {
    'use strict';

    // State
    let currentView = 'dir';   // 'dir' or 'file'
    let currentPath = '/';     // current filesystem path
    let startupCwd = '/';
    let mdRawMode = false;     // true = show raw text for markdown/mermaid files
    let currentFileInfo = null; // current file info for toggle re-render
    let mermaidLoading = null;  // promise for lazy-loading mermaid.js
    let uploading = false;      // true while upload in progress

    // Selection mode state
    let selectionMode = false;
    let selectedPaths = new Set();
    let currentDirData = null;  // cache for re-rendering rows

    // Sibling navigation state (prev/next file in the current directory)
    let siblingFiles = [];      // [{name, path}] — files only, in listing order
    let siblingIndex = -1;      // position of current file in siblingFiles
    let siblingDir = null;      // parent dir path the siblings were loaded from

    // SVG icon fragments (Lucide)
    const ICON_FOLDER = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
    const ICON_FILE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>';
    const ICON_CHECK = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
    const ICON_X = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
    const ICON_CIRCLE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/></svg>';
    const ICON_CHECK_CIRCLE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>';
    const ICON_AUDIO_NOTE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
    const ICON_AUDIO_STOP = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>';

    // Inline audio audition: at most one sample plays at a time, straight from
    // the listing, without navigating. State lives in audio-audition.js; the
    // onStart/onStop callbacks translate it into per-row button classes.
    const audition = MerlinAudioAudition.createAuditionController({
        createAudio: (src) => new Audio(src),
        onStart: (path) => setRowPlaying(path, true),
        onStop: (path) => setRowPlaying(path, false),
    });

    // DOM refs
    let dirView, fileView;
    let dirHeader, selectionHeader, deleteConfirmBar;
    let breadcrumbs, dirEntries, dirEmpty, dirError, dirErrorMsg, dirLoading;
    let fileMeta, fileContent, fileLoading, wrapToggle, downloadLink;
    let gitCommitsBtn, mdToggle;
    let uploadBtn, uploadInput, uploadProgress, uploadProgressFill, uploadProgressText;
    let createBtn, selectBtn;
    let selectionCloseBtn, selectionCount, downloadSelBtn, renameBtn, deleteSelBtn;
    let deleteConfirmMsg, deleteCancelBtn, deleteConfirmBtn;
    let downloadDirBtn;
    let fileRenameBtn, fileDeleteBtn;
    let fileDeleteConfirm, fileDeleteCancel, fileDeleteGo;
    let fileHeader, fileActions;
    let filePrevBtn, fileNextBtn, fileCounter, fileNavCluster;

    // ---------------------------------------------------------------------------
    // Toast notifications
    // ---------------------------------------------------------------------------

    const Toast = {
        _container: null,
        _getContainer() {
            if (!this._container) {
                this._container = document.createElement('div');
                this._container.className = 'toast-container';
                document.body.appendChild(this._container);
            }
            return this._container;
        },
        show(message, type) {
            type = type || 'success';
            const toast = document.createElement('div');
            toast.className = 'toast toast-' + type;
            toast.textContent = message;
            const dismiss = document.createElement('button');
            dismiss.className = 'toast-dismiss';
            dismiss.innerHTML = ICON_X;
            dismiss.addEventListener('click', () => toast.remove());
            toast.appendChild(dismiss);
            this._getContainer().appendChild(toast);
            setTimeout(() => { if (toast.parentNode) toast.remove(); }, 3000);
        }
    };

    // ---------------------------------------------------------------------------
    // Dropdown
    // ---------------------------------------------------------------------------

    const Dropdown = {
        _active: null,
        _backdrop: null,
        _onEscape: null,

        show(trigger, items, onSelect) {
            this.dismiss();

            const rect = trigger.getBoundingClientRect();
            const menu = document.createElement('div');
            menu.className = 'dropdown-menu';
            menu.setAttribute('role', 'menu');

            items.forEach((item) => {
                const el = document.createElement('button');
                el.className = 'dropdown-item';
                if (item.danger) el.classList.add('dropdown-item-danger');
                el.setAttribute('role', 'menuitem');
                el.innerHTML = '<span class="dropdown-item-icon">' + item.icon + '</span> ' + item.label;
                el.addEventListener('click', () => { this.dismiss(); onSelect(item.value); });
                menu.appendChild(el);
            });

            // Position below trigger, align right edge
            const top = rect.bottom + 4;
            const flipUp = top + (items.length * 44 + 8) > window.innerHeight;
            menu.style.position = 'fixed';
            menu.style.right = (window.innerWidth - rect.right) + 'px';
            menu.style.top = flipUp ? (rect.top - items.length * 44 - 8) + 'px' : top + 'px';

            const backdrop = document.createElement('div');
            backdrop.className = 'dropdown-backdrop';
            backdrop.addEventListener('click', () => this.dismiss());

            document.body.appendChild(backdrop);
            document.body.appendChild(menu);
            this._active = menu;
            this._backdrop = backdrop;

            menu.querySelector('button').focus();

            this._onEscape = (e) => {
                if (e.key === 'Escape') { this.dismiss(); trigger.focus(); }
            };
            document.addEventListener('keydown', this._onEscape);
        },

        dismiss() {
            if (this._active) { this._active.remove(); this._active = null; }
            if (this._backdrop) { this._backdrop.remove(); this._backdrop = null; }
            if (this._onEscape) { document.removeEventListener('keydown', this._onEscape); this._onEscape = null; }
        }
    };

    // ---------------------------------------------------------------------------
    // Init
    // ---------------------------------------------------------------------------

    document.addEventListener('DOMContentLoaded', () => {
        const app = document.getElementById('files-app');
        startupCwd = app.dataset.startupCwd || '/';

        dirView = document.getElementById('dir-view');
        fileView = document.getElementById('file-view');
        dirHeader = document.getElementById('dir-header');
        selectionHeader = document.getElementById('selection-header');
        deleteConfirmBar = document.getElementById('delete-confirm');
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
        gitCommitsBtn = document.getElementById('git-commits-btn');
        mdToggle = document.getElementById('md-toggle');

        uploadBtn = document.getElementById('upload-btn');
        uploadInput = document.getElementById('upload-input');
        uploadProgress = document.getElementById('upload-progress');
        uploadProgressFill = document.getElementById('upload-progress-fill');
        uploadProgressText = document.getElementById('upload-progress-text');

        createBtn = document.getElementById('create-btn');
        selectBtn = document.getElementById('select-btn');
        downloadDirBtn = document.getElementById('download-dir-btn');
        selectionCloseBtn = document.getElementById('selection-close-btn');
        selectionCount = document.getElementById('selection-count');
        downloadSelBtn = document.getElementById('download-sel-btn');
        renameBtn = document.getElementById('rename-btn');
        deleteSelBtn = document.getElementById('delete-sel-btn');
        deleteConfirmMsg = document.getElementById('delete-confirm-msg');
        deleteCancelBtn = document.getElementById('delete-cancel-btn');
        deleteConfirmBtn = document.getElementById('delete-confirm-btn');

        fileRenameBtn = document.getElementById('file-rename-btn');
        fileDeleteBtn = document.getElementById('file-delete-btn');
        fileDeleteConfirm = document.getElementById('file-delete-confirm');
        fileDeleteCancel = document.getElementById('file-delete-cancel');
        fileDeleteGo = document.getElementById('file-delete-go');
        fileHeader = document.getElementById('file-header');
        fileActions = document.getElementById('file-actions');
        filePrevBtn = document.getElementById('file-prev-btn');
        fileNextBtn = document.getElementById('file-next-btn');
        fileCounter = document.getElementById('file-counter');
        fileNavCluster = document.getElementById('file-nav-cluster');

        // Event listeners — existing
        document.getElementById('file-back-btn').addEventListener('click', goToParent);
        wrapToggle.addEventListener('click', toggleWrap);
        mdToggle.addEventListener('click', toggleMarkdown);
        uploadBtn.addEventListener('click', () => { if (!uploading) uploadInput.click(); });
        uploadInput.addEventListener('change', handleUpload);

        // Event listeners — new: create
        createBtn.addEventListener('click', () => {
            Dropdown.show(createBtn, [
                { label: 'New file', value: 'file', icon: ICON_FILE },
                { label: 'New folder', value: 'dir', icon: ICON_FOLDER },
            ], (type) => showCreateRow(type));
        });

        // Event listeners — new: selection mode
        selectBtn.addEventListener('click', () => {
            if (selectionMode) exitSelectionMode();
            else enterSelectionMode();
        });
        selectionCloseBtn.addEventListener('click', exitSelectionMode);
        downloadSelBtn.addEventListener('click', handleSelectionDownload);
        renameBtn.addEventListener('click', handleSelectionRename);
        deleteSelBtn.addEventListener('click', handleSelectionDelete);
        downloadDirBtn.addEventListener('click', handleDirectoryDownload);
        deleteCancelBtn.addEventListener('click', () => {
            deleteConfirmBar.style.display = 'none';
            selectionHeader.style.display = 'flex';
        });
        deleteConfirmBtn.addEventListener('click', executeDelete);

        // Event listeners — new: file viewer rename/delete
        fileRenameBtn.addEventListener('click', handleFileViewerRename);
        fileDeleteBtn.addEventListener('click', () => {
            fileHeader.style.display = 'none';
            fileDeleteConfirm.style.display = 'flex';
        });
        fileDeleteCancel.addEventListener('click', () => {
            fileDeleteConfirm.style.display = 'none';
            fileHeader.style.display = 'flex';
        });
        fileDeleteGo.addEventListener('click', handleFileViewerDelete);

        // Event listeners — sibling navigation
        filePrevBtn.addEventListener('click', () => navigateSibling(-1));
        fileNextBtn.addEventListener('click', () => navigateSibling(1));

        // Event listeners — empty state buttons
        document.getElementById('empty-create-btn').addEventListener('click', () => showCreateRow('file'));
        document.getElementById('empty-upload-btn').addEventListener('click', () => { if (!uploading) uploadInput.click(); });

        // Global keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            // Ignore keys when typing in an input
            const tag = (e.target && e.target.tagName) || '';
            if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target && e.target.isContentEditable)) return;

            if (e.key === 'Escape') {
                if (selectionMode) { exitSelectionMode(); return; }
                if (currentView === 'file') { goToParent(); return; }
            }
            if (currentView === 'file' && !e.ctrlKey && !e.metaKey && !e.altKey) {
                // Let audio/video controls handle arrow keys (seeking)
                if (tag === 'VIDEO' || tag === 'AUDIO') return;
                if (e.key === 'ArrowLeft') { navigateSibling(-1); e.preventDefault(); }
                else if (e.key === 'ArrowRight') { navigateSibling(1); e.preventDefault(); }
            }
        });

        // Configure marked.js
        if (typeof marked !== 'undefined') {
            marked.setOptions({ gfm: true, breaks: false });
            const renderer = new marked.Renderer();
            renderer.heading = function({ tokens, depth }) {
                const text = this.parser.parseInline(tokens);
                const raw = tokens.map(t => t.raw || t.text || '').join('');
                const id = raw.toLowerCase().replace(/[^\w\s-]/g, '').replace(/\s+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
                return '<h' + depth + ' id="' + id + '">' + text + '</h' + depth + '>\n';
            };
            marked.use({ renderer });
        }

        window.addEventListener('popstate', routeFromUrl);
        routeFromUrl();
    });

    // ---------------------------------------------------------------------------
    // Routing
    // ---------------------------------------------------------------------------

    function routeFromUrl() {
        const urlPath = window.location.pathname;
        if (urlPath.startsWith('/files/')) {
            browse('/' + urlPath.slice(7), false);
            return;
        }
        const saved = localStorage.getItem('merlin-files-path');
        const defaultPath = (saved && saved !== '/') ? saved : startupCwd;
        if (defaultPath && defaultPath !== '/') {
            navigateTo(defaultPath, true);
        } else {
            browse('/', false);
        }
    }

    function navigateTo(fsPath, pushState) {
        const urlPath = fsPath === '/' ? '/files' : '/files' + fsPath;
        if (pushState && window.location.pathname !== urlPath) {
            history.pushState(null, '', urlPath);
        }
        browse(fsPath, false);
    }

    function goToParent() {
        const parent = currentPath.replace(/\/[^/]*\/?$/, '') || '/';
        navigateTo(parent, true);
    }

    // ---------------------------------------------------------------------------
    // Browse (single entry point — handles both dirs and files)
    // ---------------------------------------------------------------------------

    async function browse(fsPath, pushState) {
        currentPath = fsPath;

        // Leaving the current listing (into a subdir or a file detail) stops any
        // inline audition; the row it belongs to is about to disappear.
        audition.stop();

        // Exit selection mode when browsing
        if (selectionMode) exitSelectionMode();

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
            currentDirData = data;
            setSiblingsFromDir(data, fsPath);
            renderDirectory(data);
            localStorage.setItem('merlin-files-path', fsPath);
            // Show download button except at filesystem root
            downloadDirBtn.style.display = fsPath === '/' ? 'none' : '';
            if (data.repo_root) {
                gitCommitsBtn.href = '/commits?repo=' + encodeURIComponent(data.repo_root);
                gitCommitsBtn.style.display = '';
            } else {
                gitCommitsBtn.style.display = 'none';
            }
        } else if (data.type === 'file') {
            showFileView();
            renderFileViewer(data);
            ensureSiblings(parentOf(data.path), data.path).then(updateSiblingUI);
        }
    }

    // ---------------------------------------------------------------------------
    // Sibling navigation (prev/next file in the current directory)
    // ---------------------------------------------------------------------------

    function parentOf(fsPath) {
        const idx = fsPath.lastIndexOf('/');
        if (idx <= 0) return '/';
        return fsPath.slice(0, idx);
    }

    function setSiblingsFromDir(data, dirPath) {
        siblingDir = dirPath;
        siblingFiles = (data.entries || [])
            .filter(e => e.type === 'file')
            .map(e => ({
                name: e.name,
                path: dirPath === '/' ? '/' + e.name : dirPath + '/' + e.name,
            }));
        siblingIndex = -1;
    }

    async function ensureSiblings(parentDir, filePath) {
        if (siblingDir !== parentDir || siblingFiles.length === 0) {
            try {
                const resp = await fetch('/api/files/browse?path=' + encodeURIComponent(parentDir));
                if (!resp.ok) { siblingFiles = []; siblingIndex = -1; siblingDir = null; return; }
                const data = await resp.json();
                if (data.type !== 'directory') { siblingFiles = []; siblingIndex = -1; siblingDir = null; return; }
                setSiblingsFromDir(data, parentDir);
            } catch {
                siblingFiles = []; siblingIndex = -1; siblingDir = null;
                return;
            }
        }
        siblingIndex = siblingFiles.findIndex(f => f.path === filePath);
    }

    function navigateSibling(delta) {
        if (siblingIndex < 0 || siblingFiles.length === 0) return;
        const next = siblingIndex + delta;
        if (next < 0 || next >= siblingFiles.length) {
            shakeBoundary(delta < 0 ? filePrevBtn : fileNextBtn);
            return;
        }
        navigateTo(siblingFiles[next].path, true);
    }

    function shakeBoundary(btn) {
        btn.classList.remove('at-boundary');
        void btn.offsetWidth;  // restart the animation if re-triggered
        btn.classList.add('at-boundary');
    }

    function updateSiblingUI() {
        const hasSiblings = siblingIndex >= 0 && siblingFiles.length > 1;
        if (!hasSiblings) {
            fileNavCluster.style.display = 'none';
            return;
        }
        fileNavCluster.style.display = '';
        const total = siblingFiles.length;
        const pos = siblingIndex + 1;
        fileCounter.textContent = pos + ' / ' + total;
        fileCounter.setAttribute('aria-label', 'File ' + pos + ' of ' + total);
        filePrevBtn.disabled = siblingIndex === 0;
        fileNextBtn.disabled = siblingIndex === total - 1;
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
        const audio = fileContent.querySelector('audio');
        if (audio) audio.pause();
        const video = fileContent.querySelector('video');
        if (video) video.pause();
        dirView.style.display = '';
        fileView.style.display = 'none';
        fileDeleteConfirm.style.display = 'none';
        fileHeader.style.display = 'flex';
    }

    function showFileView() {
        currentView = 'file';
        dirView.style.display = 'none';
        fileView.style.display = '';
        fileDeleteConfirm.style.display = 'none';
        fileHeader.style.display = 'flex';
    }

    // ---------------------------------------------------------------------------
    // Selection mode
    // ---------------------------------------------------------------------------

    function enterSelectionMode() {
        selectionMode = true;
        selectedPaths.clear();
        selectBtn.classList.add('active');
        dirHeader.style.display = 'none';
        selectionHeader.style.display = 'flex';
        deleteConfirmBar.style.display = 'none';
        updateSelectionUI();
        rerenderRows();
    }

    function exitSelectionMode() {
        selectionMode = false;
        selectedPaths.clear();
        selectBtn.classList.remove('active');
        dirHeader.style.display = 'flex';
        selectionHeader.style.display = 'none';
        deleteConfirmBar.style.display = 'none';
        rerenderRows();
    }

    function updateSelectionUI() {
        const count = selectedPaths.size;
        selectionCount.textContent = count + ' selected';
        downloadSelBtn.style.display = count > 0 ? '' : 'none';
        renameBtn.style.display = count === 1 ? '' : 'none';
        deleteSelBtn.style.display = count > 0 ? '' : 'none';
    }

    // ---------------------------------------------------------------------------
    // Breadcrumbs
    // ---------------------------------------------------------------------------

    function renderBreadcrumbs(fsPath) {
        breadcrumbs.innerHTML = '';

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
        currentDirData = data;
        rerenderRows();
    }

    function rerenderRows() {
        dirEntries.innerHTML = '';

        if (!currentDirData || currentDirData.entries.length === 0) {
            dirEmpty.style.display = '';
            return;
        }
        dirEmpty.style.display = 'none';

        for (const entry of currentDirData.entries) {
            const entryPath = currentPath === '/'
                ? '/' + entry.name
                : currentPath + '/' + entry.name;

            const row = document.createElement('div');
            row.className = 'dir-entry';
            if (entry.is_hidden) row.classList.add('dir-entry-hidden');
            if (selectedPaths.has(entryPath)) row.classList.add('selected');

            // Selection checkbox
            if (selectionMode) {
                const cb = document.createElement('span');
                const isChecked = selectedPaths.has(entryPath);
                cb.className = 'select-checkbox' + (isChecked ? ' checked' : '');
                cb.setAttribute('role', 'checkbox');
                cb.setAttribute('aria-checked', isChecked ? 'true' : 'false');
                cb.setAttribute('aria-label', 'Select ' + entry.name);
                cb.innerHTML = isChecked ? ICON_CHECK_CIRCLE : ICON_CIRCLE;
                row.appendChild(cb);
            }

            // Icon
            let icon;
            if (isAudioName(entry.name) && entry.type === 'file' && !selectionMode) {
                // Audio rows: the leading icon is a one-tap inline play/stop
                // control. Tapping it auditions in place; the rest of the row
                // still opens the detail view via the click handler below.
                icon = makeAudioPlayControl(entryPath, entry.name);
            } else {
                icon = document.createElement('span');
                icon.className = 'dir-entry-icon';
                if (entry.type === 'dir') {
                    icon.innerHTML = ICON_FOLDER;
                    icon.classList.add('icon-folder');
                } else if (isImageName(entry.name)) {
                    icon.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>';
                    icon.classList.add('icon-image');
                } else if (isAudioName(entry.name)) {
                    icon.innerHTML = ICON_AUDIO_NOTE;
                    icon.classList.add('icon-audio');
                } else if (isVideoName(entry.name)) {
                    icon.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect width="15" height="14" x="1" y="5" rx="2" ry="2"/></svg>';
                    icon.classList.add('icon-video');
                } else {
                    icon.innerHTML = ICON_FILE;
                    icon.classList.add('icon-file');
                }
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
            if (selectionMode) {
                row.addEventListener('click', () => {
                    if (selectedPaths.has(entryPath)) selectedPaths.delete(entryPath);
                    else selectedPaths.add(entryPath);
                    updateSelectionUI();
                    rerenderRows();
                });
            } else {
                row.addEventListener('click', () => navigateTo(entryPath, true));
            }

            dirEntries.appendChild(row);
        }
    }

    // ---------------------------------------------------------------------------
    // Inline audio audition (play a sample from the listing without navigating)
    // ---------------------------------------------------------------------------

    // Build the leading icon for an audio row as a play/stop button. Clicking it
    // toggles inline playback and never navigates (stopPropagation keeps the
    // row's own click handler from firing). State is reflected from the shared
    // controller so a benign rerender restores the right button for a sample
    // that is still playing.
    function makeAudioPlayControl(entryPath, name) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'dir-entry-icon dir-entry-play icon-audio';
        btn.dataset.auditionPath = entryPath;
        btn.dataset.auditionName = name || '';
        const playing = audition.playingPath() === entryPath;
        applyPlayControlState(btn, playing);
        const rawUrl = '/api/files/raw?path=' + encodeURIComponent(entryPath);
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            audition.toggle(entryPath, rawUrl);
        });
        return btn;
    }

    function applyPlayControlState(btn, playing) {
        const name = btn.dataset.auditionName || 'audio';
        btn.innerHTML = playing ? ICON_AUDIO_STOP : ICON_AUDIO_NOTE;
        btn.classList.toggle('playing', playing);
        btn.setAttribute('aria-label', (playing ? 'Stop ' : 'Play ') + name);
        btn.setAttribute('aria-pressed', playing ? 'true' : 'false');
    }

    // Reflect a controller start/stop onto the matching row. Queried by path so
    // it works regardless of which rerender produced the current DOM.
    function setRowPlaying(path, playing) {
        const selector =
            '.dir-entry-play[data-audition-path="' + cssEscape(path) + '"]';
        const btn = dirEntries.querySelector(selector);
        if (!btn) return;
        applyPlayControlState(btn, playing);
        const row = btn.closest('.dir-entry');
        if (row) row.classList.toggle('playing', playing);
    }

    // Escape a value for use inside a CSS attribute selector. Sample paths can
    // carry quotes, brackets, and other metacharacters.
    function cssEscape(value) {
        if (window.CSS && typeof window.CSS.escape === 'function') {
            return window.CSS.escape(value);
        }
        return String(value).replace(/["\\]/g, '\\$&');
    }

    // ---------------------------------------------------------------------------
    // Create (new file / new folder)
    // ---------------------------------------------------------------------------

    function showCreateRow(type) {
        // Exit selection mode if active
        if (selectionMode) exitSelectionMode();

        // Remove any existing create row
        const existing = document.querySelector('.create-row');
        if (existing) existing.remove();

        dirEmpty.style.display = 'none';

        const row = document.createElement('div');
        row.className = 'dir-entry create-row';

        // Icon
        const icon = document.createElement('span');
        icon.className = 'dir-entry-icon';
        icon.innerHTML = type === 'dir' ? ICON_FOLDER : ICON_FILE;
        icon.classList.add(type === 'dir' ? 'icon-folder' : 'icon-file');
        row.appendChild(icon);

        // Input
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'inline-input';
        input.placeholder = type === 'dir' ? 'Folder name' : 'File name';
        input.setAttribute('aria-label', type === 'dir' ? 'New folder name' : 'New file name');
        row.appendChild(input);

        // Confirm
        const confirm = document.createElement('button');
        confirm.className = 'inline-confirm-btn';
        confirm.innerHTML = ICON_CHECK;
        confirm.setAttribute('aria-label', 'Confirm');
        row.appendChild(confirm);

        // Cancel
        const cancel = document.createElement('button');
        cancel.className = 'inline-cancel-btn';
        cancel.innerHTML = ICON_X;
        cancel.setAttribute('aria-label', 'Cancel');
        row.appendChild(cancel);

        dirEntries.prepend(row);
        input.focus();

        async function doCreate() {
            const name = input.value.trim();
            if (!name) { row.remove(); if (!currentDirData || currentDirData.entries.length === 0) dirEmpty.style.display = ''; return; }
            try {
                const resp = await fetch('/api/files/create', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ path: currentPath, name: name, type: type }),
                });
                const data = await resp.json();
                if (!resp.ok) { Toast.show(data.detail || 'Create failed', 'error'); return; }
                Toast.show('Created ' + name, 'success');
                browse(currentPath, false);
            } catch { Toast.show('Network error', 'error'); }
        }

        let committed = false;
        confirm.addEventListener('click', () => { if (!committed) { committed = true; doCreate(); } });
        cancel.addEventListener('click', () => { row.remove(); if (!currentDirData || currentDirData.entries.length === 0) dirEmpty.style.display = ''; });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !committed) { committed = true; doCreate(); }
            if (e.key === 'Escape') { row.remove(); if (!currentDirData || currentDirData.entries.length === 0) dirEmpty.style.display = ''; }
        });
    }

    // ---------------------------------------------------------------------------
    // Download helpers
    // ---------------------------------------------------------------------------

    function showZipProgress(label) {
        uploadProgress.style.display = '';
        uploadProgressFill.classList.remove('upload-progress-error');
        uploadProgressFill.classList.add('indeterminate');
        uploadProgressText.textContent = label;
    }

    function hideZipProgress() {
        uploadProgressFill.classList.remove('indeterminate');
        uploadProgress.style.display = 'none';
    }

    // Warn before a full page unload (sidebar nav to another section, refresh,
    // tab close) while a streaming zip download is in flight: unloading aborts
    // the fetch and silently loses the download. In-Files navigation uses
    // pushState (no unload), so it is unaffected and the download continues.
    var activeZipDownloads = 0;

    function warnBeforeUnload(e) {
        e.preventDefault();
        e.returnValue = '';
        return '';
    }

    function beginZipDownload() {
        activeZipDownloads++;
        if (activeZipDownloads === 1) {
            window.addEventListener('beforeunload', warnBeforeUnload);
        }
    }

    function endZipDownload() {
        activeZipDownloads = Math.max(0, activeZipDownloads - 1);
        if (activeZipDownloads === 0) {
            window.removeEventListener('beforeunload', warnBeforeUnload);
        }
    }

    function triggerDownload(paths, btn) {
        // Single file → direct download via link
        if (paths.length === 1) {
            // Check if it's a file (not directory) by looking at currentDirData
            var isFile = false;
            if (currentDirData && currentDirData.entries) {
                for (var i = 0; i < currentDirData.entries.length; i++) {
                    if (currentDirData.entries[i].path === paths[0] && currentDirData.entries[i].type === 'file') {
                        isFile = true;
                        break;
                    }
                }
            }
            if (isFile) {
                var a = document.createElement('a');
                a.href = '/api/files/raw?path=' + encodeURIComponent(paths[0]);
                a.download = paths[0].split('/').pop();
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                return;
            }
        }

        // Multiple items or directory → zip server-side. This can take a while
        // with no native browser feedback (the response is buffered into a blob
        // before the download starts), so show an in-flight indicator.
        var label;
        if (paths.length === 1) {
            var base = paths[0].replace(/\/+$/, '').split('/').pop() || paths[0];
            label = 'Zipping ' + base + '…';
        } else {
            label = 'Zipping ' + paths.length + ' items…';
        }
        if (btn) btn.classList.add('loading');
        showZipProgress(label);
        beginZipDownload();

        function done() {
            hideZipProgress();
            if (btn) btn.classList.remove('loading');
            endZipDownload();
        }

        fetch('/api/files/download', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ paths: paths }),
        }).then(function(resp) {
            if (!resp.ok) {
                return resp.json().then(function(data) {
                    Toast.show(data.detail || 'Download failed', 'error');
                });
            }
            var filename = 'download.zip';
            var disposition = resp.headers.get('content-disposition');
            if (disposition) {
                var match = disposition.match(/filename="?([^"]+)"?/);
                if (match) filename = match[1];
            }
            return resp.blob().then(function(blob) {
                var url = URL.createObjectURL(blob);
                var a = document.createElement('a');
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            });
        }).catch(function() {
            Toast.show('Download failed', 'error');
        }).finally(done);
    }

    function handleDirectoryDownload() {
        triggerDownload([currentPath], downloadDirBtn);
    }

    function handleSelectionDownload() {
        var paths = [...selectedPaths];
        if (paths.length === 0) return;
        triggerDownload(paths, downloadSelBtn);
    }

    // ---------------------------------------------------------------------------
    // Selection mode: Rename
    // ---------------------------------------------------------------------------

    function handleSelectionRename() {
        const path = [...selectedPaths][0];
        const name = path.replace(/\/+$/, '').split('/').pop();
        exitSelectionMode();

        // Find the row and make it editable
        requestAnimationFrame(() => startInlineRename(path, name));
    }

    function startInlineRename(path, currentName) {
        const rows = dirEntries.querySelectorAll('.dir-entry');
        for (const row of rows) {
            const nameEl = row.querySelector('.dir-entry-name');
            if (!nameEl) continue;
            const displayName = nameEl.textContent.replace(/\/$/, '');
            if (displayName !== currentName) continue;

            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'inline-input';
            input.value = currentName;
            input.setAttribute('aria-label', 'Rename to');
            nameEl.replaceWith(input);
            input.focus();
            input.select();

            let committed = false;
            async function doRename() {
                if (committed) return;
                committed = true;
                const newName = input.value.trim();
                if (!newName || newName === currentName) { browse(currentPath, false); return; }
                try {
                    const resp = await fetch('/api/files/rename', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ path: path, new_name: newName }),
                    });
                    const data = await resp.json();
                    if (!resp.ok) { Toast.show(data.detail || 'Rename failed', 'error'); browse(currentPath, false); return; }
                    Toast.show('Renamed to ' + newName, 'success');
                    browse(currentPath, false);
                } catch { Toast.show('Network error', 'error'); browse(currentPath, false); }
            }

            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') doRename();
                if (e.key === 'Escape') { committed = true; browse(currentPath, false); }
            });
            input.addEventListener('blur', () => { setTimeout(doRename, 100); });
            break;
        }
    }

    // ---------------------------------------------------------------------------
    // Selection mode: Delete
    // ---------------------------------------------------------------------------

    function handleSelectionDelete() {
        const count = selectedPaths.size;
        const paths = [...selectedPaths];
        const hasDirs = paths.some(p => {
            if (!currentDirData) return false;
            const entryName = p.replace(/\/+$/, '').split('/').pop();
            const entry = currentDirData.entries.find(e => e.name === entryName);
            return entry && entry.type === 'dir';
        });

        let msg = 'Delete ' + count + ' item' + (count > 1 ? 's' : '') + '?';
        if (count === 1) {
            msg = 'Delete "' + paths[0].replace(/\/+$/, '').split('/').pop() + '"?';
        }
        if (hasDirs) msg += ' (includes folders and contents)';

        deleteConfirmMsg.textContent = msg;
        selectionHeader.style.display = 'none';
        deleteConfirmBar.style.display = 'flex';
    }

    async function executeDelete() {
        const paths = [...selectedPaths];
        let errors = 0;
        for (const path of paths) {
            try {
                const resp = await fetch('/api/files/delete', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ path: path }),
                });
                if (!resp.ok) errors++;
            } catch { errors++; }
        }
        exitSelectionMode();
        if (errors === 0) {
            Toast.show('Deleted ' + paths.length + ' item' + (paths.length > 1 ? 's' : ''), 'success');
        } else {
            Toast.show('Deleted with ' + errors + ' error' + (errors > 1 ? 's' : ''), 'error');
        }
        browse(currentPath, false);
    }

    // ---------------------------------------------------------------------------
    // File viewer: Rename
    // ---------------------------------------------------------------------------

    function handleFileViewerRename() {
        if (!currentFileInfo) return;
        const nameEl = fileMeta.querySelector('.file-meta-path');
        if (!nameEl) return;
        const currentName = currentFileInfo.name;

        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'inline-input';
        input.value = currentName;
        input.setAttribute('aria-label', 'Rename file');
        nameEl.replaceWith(input);
        input.focus();
        input.select();

        fileRenameBtn.style.display = 'none';

        let committed = false;
        async function doRename() {
            if (committed) return;
            committed = true;
            const newName = input.value.trim();
            if (!newName || newName === currentName) {
                fileMeta.innerHTML = '<div class="file-meta-path">' + esc(currentName) + '</div>';
                fileRenameBtn.style.display = '';
                return;
            }
            try {
                const resp = await fetch('/api/files/rename', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ path: currentFileInfo.path, new_name: newName }),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    Toast.show(data.detail || 'Rename failed', 'error');
                    fileMeta.innerHTML = '<div class="file-meta-path">' + esc(currentName) + '</div>';
                    fileRenameBtn.style.display = '';
                    return;
                }
                Toast.show('Renamed to ' + newName, 'success');
                const parent = currentFileInfo.path.replace(/\/[^/]*$/, '');
                navigateTo(parent + '/' + newName, true);
            } catch {
                Toast.show('Network error', 'error');
                fileMeta.innerHTML = '<div class="file-meta-path">' + esc(currentName) + '</div>';
                fileRenameBtn.style.display = '';
            }
        }

        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') doRename();
            if (e.key === 'Escape') {
                committed = true;
                fileMeta.innerHTML = '<div class="file-meta-path">' + esc(currentName) + '</div>';
                fileRenameBtn.style.display = '';
            }
        });
        input.addEventListener('blur', () => { setTimeout(doRename, 100); });
    }

    // ---------------------------------------------------------------------------
    // File viewer: Delete
    // ---------------------------------------------------------------------------

    async function handleFileViewerDelete() {
        if (!currentFileInfo) return;
        try {
            const resp = await fetch('/api/files/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ path: currentFileInfo.path }),
            });
            if (!resp.ok) {
                const data = await resp.json();
                Toast.show(data.detail || 'Delete failed', 'error');
                fileDeleteConfirm.style.display = 'none';
                fileHeader.style.display = 'flex';
                return;
            }
            Toast.show('Deleted ' + currentFileInfo.name, 'success');
            goToParent();
        } catch {
            Toast.show('Network error', 'error');
            fileDeleteConfirm.style.display = 'none';
            fileHeader.style.display = 'flex';
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
        mdToggle.style.display = 'none';
        fileLoading.style.display = 'none';
        mdRawMode = false;
        currentFileInfo = info;

        // Tear down any previous 3D scene so it doesn't leak GPU/DOM
        if (window.merlin3D && typeof window.merlin3D.disposeThreeContext === 'function') {
            window.merlin3D.disposeThreeContext();
        }

        // Clear stale sibling UI; updateSiblingUI() will repopulate after ensureSiblings resolves
        fileNavCluster.style.display = 'none';

        // Meta — title on the inner div so hover/long-press reveals the full path
        fileMeta.innerHTML = '<div class="file-meta-path" title="' + esc(info.path) + '">' + esc(info.name) + '</div>';

        // Download link
        downloadLink.href = '/api/files/raw?path=' + encodeURIComponent(info.path);
        downloadLink.download = info.name;
        downloadLink.style.display = '';

        // Show rename/delete buttons
        fileRenameBtn.style.display = '';
        fileDeleteBtn.style.display = '';

        if (info.is_image) {
            renderImagePreview(info);
        } else if (info.is_audio) {
            renderAudioPreview(info);
        } else if (info.is_video) {
            renderVideoPreview(info);
        } else if (info.is_3d_model) {
            await render3DPreview(info);
        } else if (info.is_text && isMarkdown(info.name)) {
            mdToggle.style.display = '';
            mdToggle.textContent = 'Raw';
            await renderMarkdownFile(info);
        } else if (info.is_text && isMermaidFile(info.name)) {
            mdToggle.style.display = '';
            mdToggle.textContent = 'Raw';
            await renderMermaidFile(info);
        } else if (info.is_text) {
            wrapToggle.style.display = '';
            await renderTextFile(info);
        } else {
            renderBinaryInfo(info);
        }
    }

    async function render3DPreview(info) {
        if (!window.merlin3D || typeof window.merlin3D.render3DPreview !== 'function') {
            // Module failed to load (offline, JS error) — fall back to binary
            renderBinaryInfo(info);
            return;
        }
        fileLoading.style.display = '';
        try {
            await window.merlin3D.render3DPreview(info, fileContent);
        } catch (err) {
            console.error('3D preview failed:', err);
            // Wipe any partial DOM, fall back to binary info with the file's metadata
            fileContent.innerHTML = '';
            renderBinaryInfo(info);
        } finally {
            fileLoading.style.display = 'none';
        }
    }

    // ---------------------------------------------------------------------------
    // Markdown rendering
    // ---------------------------------------------------------------------------

    async function renderMarkdownFile(info) {
        if (typeof marked === 'undefined') {
            wrapToggle.style.display = '';
            await renderTextFile(info);
            return;
        }

        fileLoading.style.display = '';

        const data = await API.get('/api/files/content?path=' + encodeURIComponent(info.path));
        fileLoading.style.display = 'none';

        if (!data) {
            fileContent.innerHTML = '<div class="file-error">Failed to load file content</div>';
            return;
        }

        let html = marked.parse(data.content);

        const fileDir = getFileDir(info.path);
        html = resolveImages(html, fileDir);
        html = resolveLinks(html, fileDir);

        const container = document.createElement('div');
        container.className = 'markdown-body';
        container.innerHTML = html;

        const mermaidBlocks = container.querySelectorAll('pre code.language-mermaid');
        for (const block of mermaidBlocks) {
            const code = block.textContent;
            const pre = block.closest('pre');
            await renderMermaidBlock(pre, code);
        }

        const mmdImgs = container.querySelectorAll('img[src*=".mmd"]');
        for (const img of mmdImgs) {
            const src = img.getAttribute('src');
            const match = src.match(/[?&]path=([^&]+)/);
            if (!match) continue;
            const mmdPath = decodeURIComponent(match[1]);
            const mmdData = await API.get('/api/files/content?path=' + encodeURIComponent(mmdPath));
            if (mmdData && mmdData.content) {
                await renderMermaidBlock(img, mmdData.content);
            }
        }

        if (typeof hljs !== 'undefined') {
            container.querySelectorAll('pre code').forEach(block => {
                if (!block.classList.contains('language-mermaid')) {
                    hljs.highlightElement(block);
                }
            });
        }

        container.querySelectorAll('a[data-internal]').forEach(link => {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                const target = link.getAttribute('data-internal');
                navigateTo(target, true);
            });
        });

        container.querySelectorAll('a[href^="#"]').forEach(link => {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                const id = link.getAttribute('href').slice(1);
                const el = container.querySelector('#' + CSS.escape(id));
                if (el) el.scrollIntoView({ behavior: 'smooth' });
            });
        });

        fileContent.appendChild(container);

        if (data.truncated) {
            const notice = document.createElement('div');
            notice.className = 'truncation-notice';
            notice.innerHTML = 'File truncated at 2 MB. <a href="/api/files/raw?path=' +
                encodeURIComponent(info.path) + '">Download full file</a>';
            fileContent.appendChild(notice);
        }
    }

    // ---------------------------------------------------------------------------
    // Mermaid file rendering
    // ---------------------------------------------------------------------------

    async function renderMermaidFile(info) {
        fileLoading.style.display = '';

        const data = await API.get('/api/files/content?path=' + encodeURIComponent(info.path));
        fileLoading.style.display = 'none';

        if (!data) {
            fileContent.innerHTML = '<div class="file-error">Failed to load file content</div>';
            return;
        }

        const placeholder = document.createElement('div');
        placeholder.className = 'mermaid-view';
        fileContent.appendChild(placeholder);

        await renderMermaidBlock(placeholder, data.content);
    }

    function getFileDir(filePath) {
        const idx = filePath.lastIndexOf('/');
        return idx > 0 ? filePath.slice(0, idx) : '/';
    }

    function resolveImages(html, fileDir) {
        return html.replace(
            /(<img\s[^>]*?)src="(?!https?:\/\/|data:)([^"]+)"/g,
            (match, prefix, src) => {
                const resolved = resolvePath(fileDir, src);
                return prefix + 'src="/api/files/raw?path=' + encodeURIComponent(resolved) + '"';
            }
        );
    }

    function resolveLinks(html, fileDir) {
        return html.replace(
            /(<a\s[^>]*?)href="([^"]+)"/g,
            (match, prefix, href) => {
                if (href.startsWith('#')) return match;
                if (/^https?:\/\//.test(href) || href.startsWith('mailto:')) {
                    return prefix + 'href="' + href + '" target="_blank" rel="noopener"';
                }
                const resolved = resolvePath(fileDir, href);
                const urlPath = '/files' + resolved;
                return prefix + 'href="' + urlPath + '" data-internal="' + esc(resolved) + '"';
            }
        );
    }

    function resolvePath(dir, relative) {
        if (relative.startsWith('/')) return relative;
        if (relative.startsWith('./')) relative = relative.slice(2);
        const parts = dir.split('/').filter(Boolean);
        const relParts = relative.split('/');
        for (const part of relParts) {
            if (part === '..') parts.pop();
            else if (part && part !== '.') parts.push(part);
        }
        return '/' + parts.join('/');
    }

    function isMarkdown(name) {
        const lower = name.toLowerCase();
        return lower.endsWith('.md') || lower.endsWith('.markdown');
    }

    function isMermaidFile(name) {
        return name.toLowerCase().endsWith('.mmd');
    }

    // ---------------------------------------------------------------------------
    // Mermaid lazy loading
    // ---------------------------------------------------------------------------

    function loadMermaid() {
        if (typeof mermaid !== 'undefined') return Promise.resolve();
        if (mermaidLoading) return mermaidLoading;

        mermaidLoading = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = '/static/vendor/mermaid.min.js';
            script.onload = () => {
                mermaid.initialize({
                    startOnLoad: false,
                    theme: 'dark',
                    themeVariables: {
                        darkMode: true,
                        background: '#1a1d27',
                        primaryColor: '#2a2e3d',
                        primaryTextColor: '#e4e6ed',
                        primaryBorderColor: '#2e3347',
                        lineColor: '#8b8fa3',
                        secondaryColor: '#222633',
                        tertiaryColor: '#2a2e3d',
                    },
                    fontFamily: "'Geist Mono', monospace",
                });
                resolve();
            };
            script.onerror = () => {
                mermaidLoading = null;
                reject(new Error('Failed to load mermaid.js'));
            };
            document.head.appendChild(script);
        });
        return mermaidLoading;
    }

    let mermaidIdCounter = 0;

    async function renderMermaidBlock(placeholder, code) {
        const extraClass = placeholder.className || '';
        try {
            await loadMermaid();
            const id = 'mermaid-' + (++mermaidIdCounter);
            const { svg } = await mermaid.render(id, code);
            const wrapper = document.createElement('div');
            wrapper.className = ('mermaid-diagram ' + extraClass).trim();
            wrapper.innerHTML = svg;
            placeholder.replaceWith(wrapper);
        } catch (err) {
            const pre = document.createElement('pre');
            pre.className = ('mermaid-error ' + extraClass).trim();
            const errMsg = document.createElement('div');
            errMsg.className = 'mermaid-error-msg';
            errMsg.textContent = 'Mermaid rendering failed';
            pre.appendChild(errMsg);
            const codeEl = document.createElement('code');
            codeEl.textContent = code;
            pre.appendChild(codeEl);
            placeholder.replaceWith(pre);
        }
    }

    // ---------------------------------------------------------------------------
    // Markdown toggle
    // ---------------------------------------------------------------------------

    async function toggleMarkdown() {
        if (!currentFileInfo) return;

        mdRawMode = !mdRawMode;
        fileContent.innerHTML = '';
        fileContent.classList.remove('wrapped');
        wrapToggle.classList.remove('active');

        if (mdRawMode) {
            mdToggle.textContent = 'Rendered';
            wrapToggle.style.display = '';
            await renderTextFile(currentFileInfo);
        } else {
            mdToggle.textContent = 'Raw';
            wrapToggle.style.display = 'none';
            if (isMermaidFile(currentFileInfo.name)) {
                await renderMermaidFile(currentFileInfo);
            } else {
                await renderMarkdownFile(currentFileInfo);
            }
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

        if (data.truncated) {
            const notice = document.createElement('div');
            notice.className = 'truncation-notice';
            notice.innerHTML = 'File truncated at 2 MB. <a href="/api/files/raw?path=' +
                encodeURIComponent(info.path) + '">Download full file</a>';
            fileContent.appendChild(notice);
        }

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
    // Image / Audio / Video / Binary previews
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
        attachSwipeNavigation(wrapper);
    }

    function renderAudioPreview(info) {
        const wrapper = document.createElement('div');
        wrapper.className = 'audio-preview';
        const audio = document.createElement('audio');
        audio.controls = true;
        audio.src = '/api/files/raw?path=' + encodeURIComponent(info.path);
        wrapper.appendChild(audio);
        const meta = document.createElement('div');
        meta.className = 'audio-meta';
        meta.textContent = formatSize(info.size) + ' · ' + info.mime_type;
        wrapper.appendChild(meta);
        fileContent.appendChild(wrapper);
        attachSwipeNavigation(wrapper);
    }

    function renderVideoPreview(info) {
        const wrapper = document.createElement('div');
        wrapper.className = 'video-preview';
        const video = document.createElement('video');
        video.controls = true;
        video.src = '/api/files/raw?path=' + encodeURIComponent(info.path);
        wrapper.appendChild(video);
        const meta = document.createElement('div');
        meta.className = 'video-meta';
        meta.textContent = formatSize(info.size) + ' · ' + info.mime_type;
        wrapper.appendChild(meta);
        fileContent.appendChild(wrapper);
        attachSwipeNavigation(wrapper);
    }

    function renderBinaryInfo(info) {
        const wrapper = document.createElement('div');
        wrapper.className = 'binary-info';
        const iconEl = document.createElement('div');
        iconEl.className = 'binary-icon';
        iconEl.innerHTML = '<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>';
        wrapper.appendChild(iconEl);
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
        attachSwipeNavigation(wrapper);
    }

    // ---------------------------------------------------------------------------
    // Swipe navigation (media previews only — avoids conflicts with text scrolling)
    // ---------------------------------------------------------------------------

    function attachSwipeNavigation(element) {
        let startX = 0, startY = 0, startTime = 0, tracking = false;

        element.addEventListener('touchstart', (e) => {
            if (e.touches.length !== 1) { tracking = false; return; }
            // Ignore swipes that start on media controls (audio/video timeline)
            const target = e.target;
            if (target.closest && (target.closest('audio') || target.closest('video'))) {
                tracking = false;
                return;
            }
            startX = e.touches[0].clientX;
            startY = e.touches[0].clientY;
            startTime = Date.now();
            tracking = true;
        }, { passive: true });

        element.addEventListener('touchend', (e) => {
            if (!tracking || e.changedTouches.length !== 1) return;
            tracking = false;
            const dx = e.changedTouches[0].clientX - startX;
            const dy = e.changedTouches[0].clientY - startY;
            if (Date.now() - startTime > 500) return;
            if (Math.abs(dx) < 60 || Math.abs(dy) > 50) return;
            navigateSibling(dx < 0 ? 1 : -1);
        }, { passive: true });
    }

    // ---------------------------------------------------------------------------
    // Wrap toggle
    // ---------------------------------------------------------------------------

    function toggleWrap() {
        fileContent.classList.toggle('wrapped');
        wrapToggle.classList.toggle('active');
    }

    // ---------------------------------------------------------------------------
    // File upload
    // ---------------------------------------------------------------------------

    function handleUpload() {
        const files = uploadInput.files;
        if (!files || files.length === 0) return;

        uploading = true;
        uploadBtn.classList.add('disabled');
        uploadProgress.style.display = '';
        uploadProgressFill.style.width = '0%';
        uploadProgressFill.classList.remove('upload-progress-error');
        uploadProgressFill.classList.remove('indeterminate');

        const fileList = Array.from(files);
        let current = 0;

        function uploadNext() {
            if (current >= fileList.length) {
                uploading = false;
                uploadBtn.classList.remove('disabled');
                uploadProgress.style.display = 'none';
                uploadInput.value = '';
                browse(currentPath, false);
                return;
            }

            const file = fileList[current];
            const label = fileList.length > 1
                ? (current + 1) + ' / ' + fileList.length + ' · ' + file.name
                : file.name;
            uploadProgressText.textContent = label;
            uploadProgressFill.style.width = '0%';

            const formData = new FormData();
            formData.append('directory', currentPath);
            formData.append('files', file);

            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/files/upload');

            xhr.upload.onprogress = function(e) {
                if (e.lengthComputable) {
                    const pct = Math.round((e.loaded / e.total) * 100);
                    uploadProgressFill.style.width = pct + '%';
                }
            };

            xhr.onload = function() {
                if (xhr.status === 200) {
                    current++;
                    uploadNext();
                } else {
                    let msg = 'Upload failed';
                    try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
                    uploadProgressFill.classList.add('upload-progress-error');
                    uploadProgressText.textContent = msg;
                    uploading = false;
                    uploadBtn.classList.remove('disabled');
                    uploadInput.value = '';
                    setTimeout(() => { uploadProgress.style.display = 'none'; }, 3000);
                }
            };

            xhr.onerror = function() {
                uploadProgressFill.classList.add('upload-progress-error');
                uploadProgressText.textContent = 'Network error';
                uploading = false;
                uploadBtn.classList.remove('disabled');
                uploadInput.value = '';
                setTimeout(() => { uploadProgress.style.display = 'none'; }, 3000);
            };

            xhr.send(formData);
        }

        uploadNext();
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
    const AUDIO_EXTS = new Set(['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac', 'webm', 'opus']);
    const VIDEO_EXTS = new Set(['mp4', 'webm', 'ogv', 'mov', 'mkv', 'avi']);

    function isImageName(name) {
        const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
        return IMAGE_EXTS.has(ext);
    }

    function isAudioName(name) {
        const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
        return AUDIO_EXTS.has(ext);
    }

    function isVideoName(name) {
        const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
        return VIDEO_EXTS.has(ext);
    }

})();
