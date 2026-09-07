/* PDF preview (pdf.js) — module entry, exposes window.merlinPdf
 *
 * Loaded as <script type="module"> after files.js. Uses the importmap declared
 * in files.html to resolve the `pdfjs-dist` bare specifier to the vendored
 * modern ESM build. Each page is rasterised to a <canvas>, so it renders
 * identically on desktop Chrome, Android Chrome, and iOS Safari — where the
 * native <embed>/<iframe> PDF path is unreliable.
 *
 * Pages render lazily via an IntersectionObserver (and un-render out of view)
 * so a long document does not freeze a phone.
 */

import * as pdfjsLib from 'pdfjs-dist';

// The worker parses/renders off the main thread. Point it at the vendored copy
// so nothing is fetched from a CDN (offline-friendly).
pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';

const CMAP_URL = '/static/vendor/pdfjs/cmaps/';
const STANDARD_FONT_URL = '/static/vendor/pdfjs/standard_fonts/';

const MIN_ZOOM = 0.25;
const MAX_ZOOM = 5;
const ZOOM_STEP = 0.25;
const MAX_PAGE_CSS_WIDTH = 900; // cap fit-width on wide desktop panels
const DPR_CAP = 2; // cap retina backing-store cost
const RERENDER_DEBOUNCE_MS = 150;

// Test-mode handle: enables window.__merlinPdfTest for Playwright assertions.
// Initialised to null so tests can `=== null` whether or not a PDF has mounted.
if (new URLSearchParams(window.location.search).get('test') === '1') {
    window.__merlinTestMode = true;
    window.__merlinPdfTest = null;
}

let ctx = null; // active document state, see mountDocument()

async function renderPdfPreview(info, container) {
    disposePdfContext();

    const wrapper = document.createElement('div');
    wrapper.className = 'pdf-preview';
    // Own vertical panning; block the browser's own pinch-zoom so we can drive
    // zoom ourselves without zooming the whole dashboard.
    wrapper.style.touchAction = 'pan-y';
    container.appendChild(wrapper);

    const pagesEl = document.createElement('div');
    pagesEl.className = 'pdf-pages';
    wrapper.appendChild(pagesEl);

    const counter = document.createElement('div');
    counter.className = 'pdf-counter';
    counter.textContent = '…';
    wrapper.appendChild(counter);

    const zoom = document.createElement('div');
    zoom.className = 'pdf-zoom';
    const zoomOut = document.createElement('button');
    zoomOut.type = 'button';
    zoomOut.textContent = '−'; // minus sign
    zoomOut.setAttribute('aria-label', 'Zoom out');
    const zoomIn = document.createElement('button');
    zoomIn.type = 'button';
    zoomIn.textContent = '+';
    zoomIn.setAttribute('aria-label', 'Zoom in');
    zoom.appendChild(zoomOut);
    zoom.appendChild(zoomIn);
    wrapper.appendChild(zoom);

    let doc;
    try {
        const resp = await fetch(
            '/api/files/raw?path=' + encodeURIComponent(info.path),
        );
        if (!resp.ok) {
            throw new Error('HTTP ' + resp.status);
        }
        const data = await resp.arrayBuffer();
        doc = await pdfjsLib.getDocument({
            data,
            cMapUrl: CMAP_URL,
            cMapPacked: true,
            standardFontDataUrl: STANDARD_FONT_URL,
        }).promise;
    } catch (err) {
        wrapper.remove();
        // Surface a clear message; files.js falls back to renderBinaryInfo.
        if (err && err.name === 'PasswordException') {
            throw new Error('This PDF is password protected');
        }
        throw err;
    }

    await mountDocument(wrapper, pagesEl, counter, zoomOut, zoomIn, doc);
}

async function mountDocument(wrapper, pagesEl, counter, zoomOut, zoomIn, doc) {
    const numPages = doc.numPages;

    // Read page 1 to size placeholders. Most PDFs are uniform; each page's real
    // size is corrected when it actually renders (renderPage).
    const firstPage = await doc.getPage(1);
    const unscaled = firstPage.getViewport({ scale: 1 });

    const state = {
        doc,
        wrapper,
        pagesEl,
        counter,
        numPages,
        pageWidthPts: unscaled.width,
        globalAspect: unscaled.height / unscaled.width, // h / w
        zoomFactor: 1, // user zoom on top of fit-to-width
        fitScale: 1, // set by computeFitScale()
        pages: [], // {el, canvas, num, rendered, renderTask, aspect}
        observer: null,
        resizeObserver: null,
        rerenderTimer: null,
        scrollRaf: null,
        destroyed: false,
        // pinch state
        pinchStartDist: 0,
        pinchStartZoom: 1,
        // double-tap state
        lastTapTime: 0,
    };
    ctx = state;

    computeFitScale(state);

    for (let n = 1; n <= numPages; n++) {
        const pageEl = document.createElement('div');
        pageEl.className = 'pdf-page';
        pageEl.dataset.page = String(n);
        const page = {
            el: pageEl,
            canvas: null,
            num: n,
            rendered: false,
            renderTask: null,
            aspect: state.globalAspect,
        };
        sizePlaceholder(state, page);
        pagesEl.appendChild(pageEl);
        state.pages.push(page);
    }

    // Lazy render: pages within ~2 viewports render; pages outside un-render.
    state.observer = new IntersectionObserver(
        (entries) => {
            for (const entry of entries) {
                const page = state.pages[Number(entry.target.dataset.page) - 1];
                if (!page) continue;
                if (entry.isIntersecting) {
                    renderPage(state, page);
                } else {
                    unrenderPage(page);
                }
            }
        },
        { root: wrapper, rootMargin: '200% 0px' },
    );
    state.pages.forEach((p) => state.observer.observe(p.el));

    // Re-fit on panel resize / device rotation.
    state.resizeObserver = new ResizeObserver(() => {
        computeFitScale(state);
        relayout(state);
    });
    state.resizeObserver.observe(wrapper);

    // Page counter follows scroll.
    wrapper.addEventListener('scroll', () => onScroll(state), { passive: true });
    updateCounter(state);

    // Zoom controls.
    zoomOut.addEventListener('click', () => applyZoom(state, -ZOOM_STEP));
    zoomIn.addEventListener('click', () => applyZoom(state, ZOOM_STEP));
    state.onKeyDown = (e) => onKeyDown(state, e);
    document.addEventListener('keydown', state.onKeyDown);
    wrapper.addEventListener('wheel', (e) => onWheel(state, e), { passive: false });

    // Touch: pinch-to-zoom + double-tap.
    wrapper.addEventListener('touchstart', (e) => onTouchStart(state, e), {
        passive: false,
    });
    wrapper.addEventListener('touchmove', (e) => onTouchMove(state, e), {
        passive: false,
    });
    wrapper.addEventListener('touchend', (e) => onTouchEnd(state, e), {
        passive: false,
    });

    updateTestHandle(state);
}

// ---------------------------------------------------------------------------
// Layout + rendering
// ---------------------------------------------------------------------------

function cssScale(state) {
    return state.fitScale * state.zoomFactor;
}

function computeFitScale(state) {
    const avail = (state.wrapper.clientWidth || 1) - 24; // side margins
    const maxWidth = Math.min(Math.max(avail, 1), MAX_PAGE_CSS_WIDTH);
    state.fitScale = Math.max(0.1, maxWidth / state.pageWidthPts);
}

function sizePlaceholder(state, page) {
    const cssW = state.pageWidthPts * cssScale(state);
    page.el.style.width = Math.floor(cssW) + 'px';
    page.el.style.height = Math.floor(cssW * page.aspect) + 'px';
}

async function renderPage(state, page) {
    if (page.rendered || state.destroyed) return;
    page.rendered = true;

    let pdfPage;
    try {
        pdfPage = await state.doc.getPage(page.num);
    } catch (err) {
        page.rendered = false;
        return;
    }
    if (state.destroyed) return;

    const css = cssScale(state);
    const dpr = Math.min(window.devicePixelRatio || 1, DPR_CAP);
    const displayVp = pdfPage.getViewport({ scale: css });
    const renderVp = pdfPage.getViewport({ scale: css * dpr });

    // Correct placeholder + stored aspect from the real page.
    page.aspect = displayVp.height / displayVp.width;
    page.el.style.width = Math.floor(displayVp.width) + 'px';
    page.el.style.height = Math.floor(displayVp.height) + 'px';

    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(renderVp.width);
    canvas.height = Math.floor(renderVp.height);
    const canvasCtx = canvas.getContext('2d');
    page.canvas = canvas;
    page.el.appendChild(canvas);

    const task = pdfPage.render({ canvasContext: canvasCtx, viewport: renderVp });
    page.renderTask = task;
    try {
        await task.promise;
    } catch (err) {
        // Cancelled (navigation / zoom re-render) — allow a later re-render.
        if (err && err.name === 'RenderingCancelledException') {
            page.rendered = false;
            if (page.canvas && page.canvas.parentNode) {
                page.canvas.parentNode.removeChild(page.canvas);
            }
            page.canvas = null;
        }
    } finally {
        page.renderTask = null;
        updateTestHandle(state);
    }
}

function unrenderPage(page) {
    if (page.renderTask) {
        try {
            page.renderTask.cancel();
        } catch (e) {
            /* ignore */
        }
        page.renderTask = null;
    }
    if (page.canvas && page.canvas.parentNode) {
        page.canvas.parentNode.removeChild(page.canvas);
    }
    page.canvas = null;
    page.rendered = false;
}

// Re-size every placeholder and re-render the pages that currently have a
// canvas (i.e. the visible set), debounced so a zoom drag doesn't thrash.
function relayout(state) {
    for (const page of state.pages) {
        if (!page.canvas) sizePlaceholder(state, page);
    }
    if (state.rerenderTimer) clearTimeout(state.rerenderTimer);
    state.rerenderTimer = setTimeout(() => {
        state.rerenderTimer = null;
        if (state.destroyed) return;
        for (const page of state.pages) {
            if (page.canvas) {
                unrenderPage(page);
                renderPage(state, page);
            }
        }
    }, RERENDER_DEBOUNCE_MS);
}

// ---------------------------------------------------------------------------
// Zoom
// ---------------------------------------------------------------------------

function setZoom(state, next) {
    const clamped = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next));
    if (Math.abs(clamped - state.zoomFactor) < 0.001) return;
    state.zoomFactor = clamped;
    relayout(state);
    updateTestHandle(state);
}

function applyZoom(state, delta) {
    setZoom(state, state.zoomFactor + delta);
}

function onKeyDown(state, e) {
    if (state.destroyed) return;
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target?.isContentEditable) {
        return;
    }
    if (e.key === '+' || e.key === '=') {
        e.preventDefault();
        applyZoom(state, ZOOM_STEP);
    } else if (e.key === '-' || e.key === '_') {
        e.preventDefault();
        applyZoom(state, -ZOOM_STEP);
    }
}

function onWheel(state, e) {
    if (!(e.ctrlKey || e.metaKey)) return; // plain wheel = scroll
    e.preventDefault();
    applyZoom(state, e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP);
}

// ---------------------------------------------------------------------------
// Touch: pinch-to-zoom + double-tap
// ---------------------------------------------------------------------------

function touchDist(t0, t1) {
    const dx = t0.clientX - t1.clientX;
    const dy = t0.clientY - t1.clientY;
    return Math.hypot(dx, dy);
}

function onTouchStart(state, e) {
    if (e.touches.length === 2) {
        state.pinchStartDist = touchDist(e.touches[0], e.touches[1]);
        state.pinchStartZoom = state.zoomFactor;
    } else if (e.touches.length === 1) {
        // Double-tap detection.
        const now = Date.now();
        if (now - state.lastTapTime < 300) {
            e.preventDefault();
            setZoom(state, state.zoomFactor > 1.01 ? 1 : 2);
            state.lastTapTime = 0;
        } else {
            state.lastTapTime = now;
        }
    }
}

function onTouchMove(state, e) {
    if (e.touches.length === 2 && state.pinchStartDist > 0) {
        e.preventDefault(); // block browser pinch-zoom / page scroll
        const dist = touchDist(e.touches[0], e.touches[1]);
        const ratio = dist / state.pinchStartDist;
        // Live CSS-scale for feedback; committed (re-rendered sharp) on end.
        const live = Math.min(
            MAX_ZOOM,
            Math.max(MIN_ZOOM, state.pinchStartZoom * ratio),
        );
        const rel = live / state.zoomFactor;
        state.pagesEl.style.transformOrigin = 'top center';
        state.pagesEl.style.transform = 'scale(' + rel + ')';
        state.pendingPinchZoom = live;
    }
}

function onTouchEnd(state, e) {
    if (state.pinchStartDist > 0 && e.touches.length < 2) {
        state.pinchStartDist = 0;
        state.pagesEl.style.transform = '';
        if (state.pendingPinchZoom) {
            setZoom(state, state.pendingPinchZoom);
            state.pendingPinchZoom = 0;
        }
    }
}

// ---------------------------------------------------------------------------
// Page counter
// ---------------------------------------------------------------------------

function onScroll(state) {
    if (state.scrollRaf) return;
    state.scrollRaf = requestAnimationFrame(() => {
        state.scrollRaf = null;
        updateCounter(state);
    });
}

function updateCounter(state) {
    // The current page is the last one whose top is at or above the viewport top.
    const top = state.wrapper.scrollTop;
    let current = 1;
    for (const page of state.pages) {
        if (page.el.offsetTop - 16 <= top) {
            current = page.num;
        } else {
            break;
        }
    }
    state.counter.textContent = current + ' / ' + state.numPages;
}

// ---------------------------------------------------------------------------
// Test handle + dispose
// ---------------------------------------------------------------------------

function updateTestHandle(state) {
    if (!window.__merlinTestMode) return;
    window.__merlinPdfTest = {
        numPages: state.numPages,
        get renderedPageCount() {
            return state.pages.filter((p) => p.canvas).length;
        },
        get scale() {
            return cssScale(state);
        },
        get zoomFactor() {
            return state.zoomFactor;
        },
    };
}

function disposePdfContext() {
    if (!ctx) return;
    const state = ctx;
    state.destroyed = true;
    try {
        if (state.observer) state.observer.disconnect();
        if (state.resizeObserver) state.resizeObserver.disconnect();
        if (state.rerenderTimer) clearTimeout(state.rerenderTimer);
        if (state.scrollRaf) cancelAnimationFrame(state.scrollRaf);
        if (state.onKeyDown) {
            document.removeEventListener('keydown', state.onKeyDown);
        }
        for (const page of state.pages) {
            if (page.renderTask) {
                try {
                    page.renderTask.cancel();
                } catch (e) {
                    /* ignore */
                }
            }
        }
        if (state.doc) {
            state.doc.destroy();
        }
        if (state.wrapper && state.wrapper.parentNode) {
            state.wrapper.parentNode.removeChild(state.wrapper);
        }
    } finally {
        ctx = null;
        if (window.__merlinTestMode) {
            window.__merlinPdfTest = null;
        }
    }
}

// Expose to non-module files.js
window.merlinPdf = {
    renderPdfPreview,
    disposePdfContext,
};
