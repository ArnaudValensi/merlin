/* Folder picker — shared component for selecting git repositories.
   Browse directories + fuzzy search repos via fd. */

var FolderPicker = (function() {
    'use strict';

    let modal, overlay, searchInput, breadcrumbs, content, footer, useBtn, closeBtn;
    let currentPath = '/';
    let currentRepoRoot = null; // git root for current browse path
    let mode = 'browse'; // 'browse' or 'search'
    let searchTimeout = null;
    let onSelect = null; // callback(repoPath)
    let homeDir = '/';
    let triggerElement = null; // element that opened the picker, for focus restore

    function init(opts) {
        modal = document.getElementById('folder-picker-modal');
        overlay = modal.querySelector('.picker-overlay');
        searchInput = document.getElementById('picker-search');
        breadcrumbs = document.getElementById('picker-breadcrumbs');
        content = document.getElementById('picker-content');
        footer = document.getElementById('picker-footer');
        useBtn = document.getElementById('picker-use-btn');
        closeBtn = document.getElementById('picker-close');
        homeDir = opts.homeDir || '/';
        onSelect = opts.onSelect || function() {};

        closeBtn.addEventListener('click', close);
        overlay.addEventListener('click', close);
        useBtn.addEventListener('click', selectCurrentDir);
        searchInput.addEventListener('input', onSearchInput);

        // Escape key closes the modal
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape' && modal.style.display !== 'none') {
                e.stopPropagation();
                close();
            }
        });

        // Focus trap — keep Tab within the modal
        modal.addEventListener('keydown', function(e) {
            if (e.key !== 'Tab') return;
            var focusable = modal.querySelectorAll(
                'button:not([disabled]), input:not([disabled]), [tabindex="0"]'
            );
            if (focusable.length === 0) return;
            var first = focusable[0];
            var last = focusable[focusable.length - 1];
            if (e.shiftKey) {
                if (document.activeElement === first) {
                    e.preventDefault();
                    last.focus();
                }
            } else {
                if (document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                }
            }
        });
    }

    function open(startPath) {
        triggerElement = document.activeElement;
        currentPath = startPath || homeDir;
        mode = 'browse';
        searchInput.value = '';
        modal.style.display = '';
        document.body.style.overflow = 'hidden';
        browse(currentPath);
    }

    function close() {
        modal.style.display = 'none';
        document.body.style.overflow = '';
        // Restore focus to the element that opened the picker
        if (triggerElement && triggerElement.focus) {
            triggerElement.focus();
            triggerElement = null;
        }
    }

    function shortenPath(p) {
        if (homeDir && p.startsWith(homeDir)) {
            return '~' + p.slice(homeDir.length);
        }
        return p;
    }

    // Browse mode
    async function browse(path) {
        currentPath = path;
        mode = 'browse';
        content.innerHTML = '<div class="picker-loading">Loading...</div>';
        renderBreadcrumbs(path);
        footer.style.display = '';

        const data = await API.get('/api/files/browse?path=' + encodeURIComponent(path));
        if (!data || data.type !== 'directory') {
            content.innerHTML = '<div class="picker-empty">Cannot browse this path</div>';
            return;
        }

        // Track git root for "Use this folder" button
        currentRepoRoot = data.repo_root || null;
        if (currentRepoRoot) {
            useBtn.textContent = 'Use ' + shortenPath(currentRepoRoot);
            footer.style.display = '';
        } else {
            footer.style.display = 'none';
        }

        // Filter to directories only
        const dirs = data.entries.filter(function(e) { return e.type === 'dir'; });

        if (dirs.length === 0) {
            content.innerHTML = '<div class="picker-empty">No subdirectories</div>';
            return;
        }

        content.innerHTML = '';
        for (var i = 0; i < dirs.length; i++) {
            var entry = dirs[i];
            var row = document.createElement('div');
            row.className = 'picker-item';
            row.tabIndex = 0;
            row.setAttribute('role', 'button');

            var icon = document.createElement('span');
            icon.className = 'picker-item-icon';
            if (entry.has_git) {
                // Git branch icon
                icon.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>';
                icon.classList.add('icon-git');
            } else {
                // Folder icon
                icon.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
            }
            row.appendChild(icon);

            var name = document.createElement('span');
            name.className = 'picker-item-name';
            name.textContent = entry.name + '/';
            row.appendChild(name);

            var entryPath = currentPath === '/' ? '/' + entry.name : currentPath + '/' + entry.name;
            (function(p) {
                row.addEventListener('click', function() { browse(p); });
                row.addEventListener('keydown', function(e) {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); browse(p); }
                });
            })(entryPath);

            content.appendChild(row);
        }
    }

    function renderBreadcrumbs(path) {
        breadcrumbs.innerHTML = '';
        breadcrumbs.style.display = '';

        var rootLink = document.createElement('a');
        rootLink.className = 'picker-crumb';
        rootLink.textContent = '/';
        rootLink.href = '#';
        rootLink.addEventListener('click', function(e) {
            e.preventDefault();
            browse('/');
        });
        breadcrumbs.appendChild(rootLink);

        if (path === '/') return;

        var parts = path.split('/').filter(Boolean);
        var cumulative = '';
        for (var i = 0; i < parts.length; i++) {
            cumulative += '/' + parts[i];
            var sep = document.createElement('span');
            sep.className = 'picker-crumb-sep';
            sep.textContent = '/';
            breadcrumbs.appendChild(sep);

            var link = document.createElement('a');
            link.className = 'picker-crumb';
            link.textContent = parts[i];
            link.href = '#';
            if (i === parts.length - 1) link.classList.add('picker-crumb-current');
            (function(p) {
                link.addEventListener('click', function(e) {
                    e.preventDefault();
                    browse(p);
                });
            })(cumulative);
            breadcrumbs.appendChild(link);
        }
    }

    function selectCurrentDir() {
        // Use the resolved git root (same as file browser git button behavior)
        close();
        onSelect(currentRepoRoot || currentPath);
    }

    // Search mode
    function onSearchInput() {
        clearTimeout(searchTimeout);
        var q = searchInput.value.trim();
        if (!q) {
            // Switch back to browse mode
            browse(currentPath);
            return;
        }
        mode = 'search';
        breadcrumbs.style.display = 'none';
        footer.style.display = 'none';
        content.innerHTML = '<div class="picker-loading">Searching...</div>';

        searchTimeout = setTimeout(function() {
            searchRepos(q);
        }, 300);
    }

    async function searchRepos(q) {
        var data = await API.get('/api/git/repos?q=' + encodeURIComponent(q));
        if (mode !== 'search') return; // user switched back

        if (!data || data.length === 0) {
            content.innerHTML = '<div class="picker-empty">No repositories found</div>';
            return;
        }

        content.innerHTML = '';
        for (var i = 0; i < data.length; i++) {
            var repoPath = data[i];
            var row = document.createElement('div');
            row.className = 'picker-item';
            row.tabIndex = 0;
            row.setAttribute('role', 'button');

            var icon = document.createElement('span');
            icon.className = 'picker-item-icon icon-git';
            icon.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>';
            row.appendChild(icon);

            var name = document.createElement('span');
            name.className = 'picker-item-name';
            name.textContent = shortenPath(repoPath);
            row.appendChild(name);

            (function(p) {
                var handler = function() { close(); onSelect(p); };
                row.addEventListener('click', handler);
                row.addEventListener('keydown', function(e) {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(); }
                });
            })(repoPath);

            content.appendChild(row);
        }
    }

    return { init: init, open: open, close: close };
})();
