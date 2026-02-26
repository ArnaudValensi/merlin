/* Notes Editor — JS */

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

    show(message, type = 'success', duration = 4000) {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        this._getContainer().appendChild(toast);
        setTimeout(() => toast.remove(), duration);
    }
};

// ---------------------------------------------------------------------------
// Command Palette
// ---------------------------------------------------------------------------

const Palette = {
    _overlay: null,
    _input: null,
    _results: null,
    _fuse: null,
    _notes: [],
    _selectedIndex: 0,
    _contentMode: false,
    _debounceTimer: null,
    _abortController: null,

    async init() {
        this._overlay = document.getElementById('palette-overlay');
        this._input = document.getElementById('palette-input');
        this._results = document.getElementById('palette-results');
        if (!this._overlay) return;

        // Fetch notes list
        const resp = await fetch('/api/notes');
        if (resp.ok) {
            this._notes = await resp.json();
        }

        // Init fuse.js
        this._fuse = new Fuse(this._notes, {
            keys: [
                { name: 'path', weight: 0.4 },
                { name: 'title', weight: 0.3 },
                { name: 'summary', weight: 0.15 },
                { name: 'tags', weight: 0.15 },
            ],
            threshold: 0.4,
            includeScore: true,
        });

        // Events
        this._input.addEventListener('input', () => this._onInput());
        this._input.addEventListener('keydown', (e) => this._onKeydown(e));
        this._overlay.addEventListener('click', (e) => {
            if (e.target === this._overlay) this.close();
        });

        // Keyboard shortcut: Ctrl+K
        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                e.preventDefault();
                this.toggle();
            }
            if (e.key === 'Escape' && this._overlay.classList.contains('open')) {
                this.close();
            }
        });

        // Show all notes initially
        this._render(this._notes);
    },

    open() {
        this._overlay.classList.add('open');
        this._input.value = '';
        this._input.focus();
        this._selectedIndex = 0;
        this._render(this._notes);
    },

    close() {
        this._overlay.classList.remove('open');
    },

    toggle() {
        if (this._overlay.classList.contains('open')) {
            this.close();
        } else {
            this.open();
        }
    },

    _onInput() {
        const raw = this._input.value;

        if (raw.startsWith('/')) {
            this._contentMode = true;
            const searchQuery = raw.slice(1).trim();
            this._contentSearch(searchQuery);
        } else {
            this._contentMode = false;
            if (this._debounceTimer) { clearTimeout(this._debounceTimer); this._debounceTimer = null; }
            if (this._abortController) { this._abortController.abort(); this._abortController = null; }
            const query = raw.trim();
            if (!query) { this._render(this._notes, ''); return; }
            const results = this._fuse.search(query).map(r => r.item);
            this._selectedIndex = 0;
            this._render(results, query);
        }
    },

    _contentSearch(query) {
        if (this._debounceTimer) clearTimeout(this._debounceTimer);
        if (this._abortController) { this._abortController.abort(); this._abortController = null; }

        if (query.length < 2) {
            this._results.innerHTML = '<div class="palette-hint">Type at least 2 characters...</div>';
            return;
        }

        // Show loading state
        this._results.classList.add('palette-loading');

        this._debounceTimer = setTimeout(async () => {
            this._abortController = new AbortController();
            try {
                const resp = await fetch(`/api/notes/search?q=${encodeURIComponent(query)}`, {
                    signal: this._abortController.signal,
                });
                if (!resp.ok) { this._results.innerHTML = '<div class="palette-empty">Search failed</div>'; return; }
                const data = await resp.json();
                this._renderContentResults(data, query);
            } catch (e) {
                if (e.name !== 'AbortError') {
                    this._results.innerHTML = '<div class="palette-empty">Search failed</div>';
                }
            } finally {
                this._results.classList.remove('palette-loading');
            }
        }, 300);
    },

    _escapeRegex(str) {
        return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    },

    _escapeHtml(str) {
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    },

    _findMatchPositions(line, query) {
        const lower = line.toLowerCase(), qLower = query.toLowerCase();
        const positions = [];
        let qi = 0;
        for (let i = 0; i < lower.length && qi < qLower.length; i++) {
            if (lower[i] === qLower[qi]) { positions.push(i); qi++; }
        }
        return positions;
    },

    _trimAroundMatch(line, query, maxLen = 80) {
        if (line.length <= maxLen) return line;
        // Center on the midpoint of matched character positions
        const positions = this._findMatchPositions(line, query);
        const center = positions.length
            ? positions[Math.floor(positions.length / 2)]
            : 0;
        let start = Math.max(0, center - Math.floor(maxLen / 2));
        let end = Math.min(line.length, start + maxLen);
        if (end - start < maxLen) start = Math.max(0, end - maxLen);
        let trimmed = line.slice(start, end);
        if (start > 0) trimmed = '\u2026' + trimmed;
        if (end < line.length) trimmed = trimmed + '\u2026';
        return trimmed;
    },

    _highlightMatch(line, query) {
        const trimmed = this._trimAroundMatch(line, query);
        const escaped = this._escapeHtml(trimmed);
        // Highlight each query char in order (subsequence)
        const qLower = query.toLowerCase();
        let qi = 0;
        let html = '';
        for (let i = 0; i < escaped.length; i++) {
            if (qi < qLower.length && escaped[i].toLowerCase() === qLower[qi]) {
                html += `<mark>${escaped[i]}</mark>`;
                qi++;
            } else {
                html += escaped[i];
            }
        }
        return html;
    },

    _renderContentResults(data, query) {
        if (!data.results.length) {
            this._results.innerHTML = '<div class="palette-empty">No matches found</div>';
            return;
        }

        this._selectedIndex = 0;
        const html = data.results.map((r, i) => {
            const before = r.context_before ? `<div class="palette-context-line">${this._escapeHtml(r.context_before)}</div>` : '';
            const after = r.context_after ? `<div class="palette-context-line">${this._escapeHtml(r.context_after)}</div>` : '';
            return `
            <div class="palette-item palette-content-item${i === 0 ? ' selected' : ''}"
                 data-path="${r.path}" onclick="Palette._navigate('${r.path}')">
                <div class="palette-content-header">
                    <span class="palette-item-path">${this._escapeHtml(r.path)}</span>
                    <span class="palette-line-number">:${r.line_number}</span>
                </div>
                <div class="palette-context">${before}<div class="palette-match-line">${this._highlightMatch(r.line, query)}</div>${after}</div>
            </div>
        `}).join('');

        this._results.innerHTML = html;
    },

    _onKeydown(e) {
        const items = this._results.querySelectorAll('.palette-item');
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            this._selectedIndex = Math.min(this._selectedIndex + 1, items.length - 1);
            this._updateSelection(items);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            this._selectedIndex = Math.max(this._selectedIndex - 1, 0);
            this._updateSelection(items);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (items[this._selectedIndex]) {
                items[this._selectedIndex].click();
            }
        }
    },

    _updateSelection(items) {
        items.forEach((el, i) => {
            el.classList.toggle('selected', i === this._selectedIndex);
        });
        if (items[this._selectedIndex]) {
            items[this._selectedIndex].scrollIntoView({ block: 'nearest' });
        }
    },

    _isValidPath(query) {
        return /^[\w\-./]+$/.test(query) && !query.includes('..');
    },

    _render(notes, query = '') {
        let html = '';
        let offset = 0;

        // Show create option if query looks like a valid path and doesn't match an existing note exactly
        if (query && this._isValidPath(query)) {
            const normalized = query.replace(/\.md$/, '');
            const exists = this._notes.some(n => n.path === normalized);
            if (!exists) {
                html += `
                    <div class="palette-item palette-create${this._selectedIndex === 0 ? ' selected' : ''}"
                         onclick="Palette._create('${normalized}')">
                        <span class="palette-item-path">+ Create ${normalized}</span>
                    </div>
                `;
                offset = 1;
            }
        }

        if (!notes.length && !html) {
            this._results.innerHTML = '<div class="palette-empty">No notes found</div>';
            return;
        }

        html += notes.map((note, i) => {
            const idx = i + offset;
            const tags = (note.tags || []).map(t => `<a href="/notes/tags/${encodeURIComponent(t)}" class="tag" onclick="event.stopPropagation()">${t}</a>`).join('');
            return `
                <div class="palette-item${idx === this._selectedIndex ? ' selected' : ''}"
                     data-path="${note.path}" onclick="Palette._navigate('${note.path}')">
                    <span class="palette-item-path">${note.path}</span>
                    ${note.summary ? `<span class="palette-item-summary">${note.summary}</span>` : ''}
                    ${tags ? `<div class="palette-item-tags">${tags}</div>` : ''}
                </div>
            `;
        }).join('');

        this._results.innerHTML = html;
    },

    _navigate(path) {
        window.location.href = '/notes/' + path;
    },

    _create(path) {
        window.location.href = '/notes/' + path + '?new=1';
    }
};

// ---------------------------------------------------------------------------
// Note Viewer / Editor
// ---------------------------------------------------------------------------

const NoteView = {
    _path: null,
    _rawContent: '',
    _isEditing: false,
    _isNew: false,
    _cm: null,  // CodeMirror instance

    async init(path, opts = {}) {
        this._path = path;

        if (opts.newNote) {
            this._isNew = true;
            // Generate frontmatter template for new note
            const slug = path.includes('/') ? path.split('/').pop() : path;
            const title = slug.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
            const today = new Date().toISOString().slice(0, 10);
            this._rawContent = `---\ntitle: ${title}\ncreated: ${today}\ntags: []\nrelated: []\nsummary: \n---\n\n# ${title}\n\n`;
            // Hide delete button for unsaved new notes
            const deleteBtn = document.getElementById('btn-delete');
            if (deleteBtn) deleteBtn.style.display = 'none';
            this._renderEdit();
            return;
        }

        // Fetch content
        const resp = await fetch(`/api/notes/${path}`);
        if (!resp.ok) return;
        const data = await resp.json();
        this._rawContent = data.content;

        // Render view mode
        this._renderView();
    },

    _parseFrontmatter(content) {
        const match = content.match(/^---\s*\n([\s\S]*?)\n---\s*\n/);
        if (!match) return { meta: {}, body: content };

        const raw = match[1];
        const body = content.slice(match[0].length);
        const meta = {};

        for (const line of raw.split('\n')) {
            const m = line.match(/^(\w+):\s*(.+)$/);
            if (m) {
                const key = m[1];
                let val = m[2].trim();
                // Parse arrays
                const arrMatch = val.match(/^\[([^\]]*)\]$/);
                if (arrMatch) {
                    val = arrMatch[1].split(',').map(s => s.trim().replace(/^['"]|['"]$/g, '')).filter(Boolean);
                }
                meta[key] = val;
            }
        }
        return { meta, body };
    },

    _resolveLinks(html) {
        // Rewrite internal links to /notes/ URLs
        // First: .md links → note pages (strip .md extension)
        html = html.replace(
            /href="(?!https?:\/\/|mailto:|#)([^"]+?)\.md"/g,
            (match, path) => {
                const currentDir = this._path.includes('/') ? this._path.split('/').slice(0, -1).join('/') : '';
                const resolved = path.startsWith('/') ? path.slice(1) : (currentDir ? currentDir + '/' + path : path);
                return `href="/notes/${resolved}"`;
            }
        );
        // Second: media/ links → serve from memory root (not relative to current note)
        html = html.replace(
            /href="(media\/[^"]+)"/g,
            (match, path) => `href="/notes/${path}"`
        );
        return html;
    },

    _resolveImages(html) {
        // Resolve relative image paths to serve through /notes/
        return html.replace(
            /src="(?!https?:\/\/|data:)([^"]+)"/g,
            (match, path) => {
                // media/ paths are always relative to memory root, not current note dir
                if (path.startsWith('media/')) {
                    return `src="/notes/${path}"`;
                }
                const currentDir = this._path.includes('/') ? this._path.split('/').slice(0, -1).join('/') : '';
                const resolved = path.startsWith('/') ? path : (currentDir ? currentDir + '/' + path : path);
                return `src="/notes/${resolved}"`;
            }
        );
    },

    _renderView() {
        this._isEditing = false;

        const headerEl = document.getElementById('note-header');
        const contentEl = document.getElementById('note-content');
        const wrapEl = document.getElementById('note-editor-wrap');

        // Always update UI state first (hide editor, show content, update toolbar)
        if (wrapEl) wrapEl.style.display = 'none';
        if (contentEl) contentEl.style.display = '';
        this._updateToolbar(false);

        // Then render content
        const { meta, body } = this._parseFrontmatter(this._rawContent);

        // Header
        if (headerEl && meta.title) {
            let headerHTML = `<h1 class="note-title">${meta.title}</h1>`;
            headerHTML += '<div class="note-meta">';
            if (meta.created) {
                headerHTML += `<span>${meta.created}</span>`;
            }
            if (meta.tags && meta.tags.length) {
                headerHTML += '<div class="note-tags">' +
                    meta.tags.map(t => `<a href="/notes/tags/${encodeURIComponent(t)}" class="tag">${t}</a>`).join('') +
                    '</div>';
            }
            headerHTML += '</div>';
            if (meta.summary) {
                headerHTML += `<p style="margin-top:8px;color:var(--text-muted);font-size:14px;">${meta.summary}</p>`;
            }
            headerEl.innerHTML = headerHTML;
            headerEl.style.display = '';
        } else if (headerEl) {
            headerEl.style.display = 'none';
        }

        // Content
        if (contentEl) {
            let html = marked.parse(body);
            html = this._resolveLinks(html);
            html = this._resolveImages(html);
            contentEl.innerHTML = html;

            // Apply syntax highlighting to code blocks
            contentEl.querySelectorAll('pre code').forEach(block => {
                hljs.highlightElement(block);
            });
        }
    },

    _renderEdit() {
        this._isEditing = true;

        const headerEl = document.getElementById('note-header');
        const contentEl = document.getElementById('note-content');
        const wrapEl = document.getElementById('note-editor-wrap');

        if (headerEl) headerEl.style.display = 'none';
        if (contentEl) contentEl.style.display = 'none';
        if (wrapEl) {
            wrapEl.style.display = 'block';

            if (!this._cm) {
                const useVim = localStorage.getItem('notes-vim') !== 'off';
                this._cm = CodeMirror(wrapEl, {
                    value: this._rawContent,
                    mode: 'markdown',
                    keyMap: useVim ? 'vim' : 'default',
                    theme: 'material-darker',
                    lineNumbers: true,
                    lineWrapping: true,
                    autofocus: true,
                });
                // jj → Escape in insert mode
                CodeMirror.Vim.map('jj', '<Esc>', 'insert');
                this._initDragDrop(wrapEl);
                this._updateVimToggle();
            } else {
                this._cm.setValue(this._rawContent);
            }
            this._cm.refresh();
            this._cm.focus();
        }

        this._updateToolbar(true);
    },

    toggleVim() {
        if (!this._cm) return;
        const isVim = this._cm.getOption('keyMap') === 'vim';
        const newMap = isVim ? 'default' : 'vim';
        this._cm.setOption('keyMap', newMap);
        localStorage.setItem('notes-vim', isVim ? 'off' : 'on');
        this._updateVimToggle();
        this._cm.focus();
    },

    _updateVimToggle() {
        const btn = document.getElementById('btn-vim-toggle');
        if (!btn || !this._cm) return;
        const isVim = this._cm.getOption('keyMap') === 'vim';
        btn.textContent = isVim ? 'VIM' : 'STD';
        btn.classList.toggle('btn-vim-active', isVim);
    },

    _updateToolbar(editing) {
        const editBtn = document.getElementById('btn-edit');
        const deleteBtn = document.getElementById('btn-delete');
        const saveBtn = document.getElementById('btn-save');
        const cancelBtn = document.getElementById('btn-cancel');
        const vimBtn = document.getElementById('btn-vim-toggle');
        if (editBtn) editBtn.style.display = editing ? 'none' : '';
        if (deleteBtn) deleteBtn.style.display = editing ? 'none' : '';
        if (saveBtn) saveBtn.style.display = editing ? '' : 'none';
        if (cancelBtn) cancelBtn.style.display = editing ? '' : 'none';
        if (vimBtn) vimBtn.style.display = editing ? '' : 'none';
    },

    edit() {
        this._renderEdit();
    },

    cancel() {
        if (this._isNew) {
            window.location.href = '/notes';
            return;
        }
        this._renderView();
    },

    async delete() {
        if (!confirm(`Delete ${this._path}?`)) return;

        try {
            const resp = await fetch(`/api/notes/${this._path}`, { method: 'DELETE' });
            const data = await resp.json();
            if (resp.ok) {
                Toast.show('Deleted');
                setTimeout(() => { window.location.href = '/notes'; }, 500);
            } else {
                Toast.show('Delete failed: ' + (data.detail || 'unknown error'), 'error');
            }
        } catch (err) {
            Toast.show('Delete failed: ' + err.message, 'error');
        }
    },

    async save() {
        if (!this._cm) return;

        const content = this._cm.getValue();
        const saveBtn = document.getElementById('btn-save');
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving...';
        }

        try {
            const resp = await fetch(`/api/notes/${this._path}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content }),
            });

            const data = await resp.json();
            if (resp.ok) {
                this._rawContent = content;
                this._renderView();

                if (data.committed && data.pushed) {
                    Toast.show('Saved, committed & pushed');
                } else if (data.committed && !data.pushed) {
                    Toast.show('Saved & committed (push failed — will retry later)', 'warning');
                } else {
                    Toast.show('Saved (no changes to commit)');
                }
            } else {
                Toast.show('Save failed: ' + (data.detail || 'unknown error'), 'error');
            }
        } catch (err) {
            Toast.show('Save failed: ' + err.message, 'error');
        } finally {
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save';
            }
        }
    },

    // Drag & drop media upload
    _initDragDrop(wrapEl) {
        if (!wrapEl || wrapEl._dragDropInit) return;
        wrapEl._dragDropInit = true;

        wrapEl.addEventListener('dragover', (e) => {
            e.preventDefault();
            wrapEl.classList.add('drag-over');
        });

        wrapEl.addEventListener('dragleave', () => {
            wrapEl.classList.remove('drag-over');
        });

        wrapEl.addEventListener('drop', async (e) => {
            e.preventDefault();
            wrapEl.classList.remove('drag-over');

            const files = e.dataTransfer.files;
            if (!files.length) return;

            for (const file of files) {
                await this._uploadFile(file);
            }
        });
    },

    async _uploadFile(file) {
        const formData = new FormData();
        formData.append('file', file);

        Toast.show(`Uploading ${file.name}...`, 'success', 2000);

        try {
            const resp = await fetch('/api/notes/upload', {
                method: 'POST',
                body: formData,
            });

            if (!resp.ok) {
                Toast.show(`Upload failed: ${file.name}`, 'error');
                return;
            }

            const data = await resp.json();
            const isImage = /\.(png|jpg|jpeg|gif|webp|svg)$/i.test(data.filename);
            const markdown = isImage
                ? `![${data.filename}](${data.path})`
                : `[${data.filename}](${data.path})`;

            // Insert at CodeMirror cursor
            if (this._cm) {
                const cursor = this._cm.getCursor();
                this._cm.replaceRange(markdown + '\n', cursor);
                this._cm.focus();
            }

            Toast.show(`Uploaded ${data.filename}`);
        } catch (err) {
            Toast.show(`Upload error: ${err.message}`, 'error');
        }
    }
};

// ---------------------------------------------------------------------------
// File upload button (mobile fallback)
// ---------------------------------------------------------------------------

function triggerUpload() {
    const input = document.getElementById('file-upload-input');
    if (input) input.click();
}

function handleFileSelect(input) {
    if (!input.files.length) return;
    for (const file of input.files) {
        NoteView._uploadFile(file);
    }
    input.value = '';
}

// ---------------------------------------------------------------------------
// Index page
// ---------------------------------------------------------------------------

const NotesIndex = {
    async init() {
        const resp = await fetch('/api/notes');
        if (!resp.ok) return;
        const notes = await resp.json();

        // Recent notes
        const recentEl = document.getElementById('notes-recent');
        if (recentEl) {
            const recent = notes.slice(0, 8);
            recentEl.innerHTML = recent.map(n => `
                <li>
                    <a href="/notes/${n.path}">${n.title}</a>
                    <div class="recent-meta">${n.path} ${n.summary ? '&mdash; ' + n.summary : ''}</div>
                </li>
            `).join('');
        }

        // Tag cloud
        const tagsEl = document.getElementById('notes-tags');
        if (tagsEl) {
            const tagCounts = {};
            for (const note of notes) {
                for (const tag of (note.tags || [])) {
                    tagCounts[tag] = (tagCounts[tag] || 0) + 1;
                }
            }
            const sorted = Object.entries(tagCounts).sort((a, b) => b[1] - a[1]);
            tagsEl.innerHTML = sorted.map(([tag, count]) =>
                `<a href="/notes/tags/${encodeURIComponent(tag)}" class="tag" title="${count} note${count > 1 ? 's' : ''}">${tag} (${count})</a>`
            ).join('');
        }

        // Stats
        const statsEl = document.getElementById('notes-stats');
        if (statsEl) {
            const kbCount = notes.filter(n => n.path.startsWith('kb/')).length;
            statsEl.textContent = `${notes.length} notes total, ${kbCount} in KB`;
        }
    }
};

// ---------------------------------------------------------------------------
// Tag page
// ---------------------------------------------------------------------------

const TagPage = {
    _tag: '',
    _notes: [],
    _sort: 'recent',

    async init(tag) {
        this._tag = tag;

        const resp = await fetch('/api/notes');
        if (!resp.ok) return;
        const allNotes = await resp.json();

        // Filter notes that have this tag
        this._notes = allNotes.filter(n =>
            (n.tags || []).some(t => t.toLowerCase() === tag.toLowerCase())
        );

        // Count connections: related[] length + tags count
        for (const note of this._notes) {
            note._connections = (note.tags || []).length + (note.related || []).length;
        }

        // Update count
        const countEl = document.getElementById('tag-count');
        if (countEl) {
            countEl.textContent = `${this._notes.length} note${this._notes.length !== 1 ? 's' : ''}`;
        }

        // Sort buttons
        document.querySelectorAll('.sort-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelector('.sort-btn.active')?.classList.remove('active');
                btn.classList.add('active');
                this._sort = btn.dataset.sort;
                this._render();
            });
        });

        this._render();
    },

    _render() {
        const notes = [...this._notes];
        const tag = this._tag;

        // Sort
        if (this._sort === 'recent') {
            notes.sort((a, b) => b.mtime - a.mtime);
        } else if (this._sort === 'name') {
            notes.sort((a, b) => a.title.localeCompare(b.title));
        } else if (this._sort === 'connections') {
            notes.sort((a, b) => b._connections - a._connections);
        }

        const listEl = document.getElementById('tag-notes');
        if (!listEl) return;

        if (!notes.length) {
            listEl.innerHTML = '<div class="tag-empty">No notes with this tag</div>';
            return;
        }

        listEl.innerHTML = notes.map(n => {
            const otherTags = (n.tags || [])
                .filter(t => t.toLowerCase() !== tag.toLowerCase())
                .map(t => `<a href="/notes/tags/${encodeURIComponent(t)}" class="tag">${t}</a>`)
                .join('');
            const date = n.created || '';
            return `
                <a href="/notes/${n.path}" class="tag-note-item">
                    <div class="tag-note-title">${n.title}</div>
                    <div class="tag-note-meta">
                        <span class="tag-note-path">${n.path}</span>
                        ${date ? `<span>${date}</span>` : ''}
                        ${n._connections ? `<span>${n._connections} connections</span>` : ''}
                    </div>
                    ${n.summary ? `<div class="tag-note-summary">${n.summary}</div>` : ''}
                    ${otherTags ? `<div class="tag-note-tags">${otherTags}</div>` : ''}
                </a>
            `;
        }).join('');
    }
};
