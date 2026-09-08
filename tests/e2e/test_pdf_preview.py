"""E2E tests for inline PDF preview (pdf.js).

Run with: uv run scripts.py test-e2e
(installs the needed browsers automatically — see cmd_test_e2e in scripts.py)

pdf.js renders to a 2D canvas (no WebGL), so — unlike the 3D suite — no
swiftshader flags are needed; headless Chromium renders it directly.
"""

import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright


FIXTURES = Path(__file__).parent.parent / "fixtures"


def _find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def test_files(tmp_path_factory):
    """A valid 2-page PDF, a corrupt PDF, and a neighbour file (for sibling
    navigation), copied into a temp dir the file browser can serve."""
    root = tmp_path_factory.mktemp("pdftest")
    shutil.copy(FIXTURES / "sample_2page.pdf", root / "sample_2page.pdf")
    shutil.copy(FIXTURES / "broken.pdf", root / "broken.pdf")
    # Sorts after sample_2page.pdf so #file-next-btn navigates to it.
    (root / "zzz.txt").write_text("neighbour\n")
    return root


@pytest.fixture(scope="module")
def server(test_files):
    """Start the Merlin server without auth on a random port."""
    port = _find_free_port()
    env = os.environ.copy()
    env["DASHBOARD_PASS"] = ""
    env["MERLIN_SAAS_TOKEN"] = ""
    env["DISCORD_BOT_TOKEN"] = ""
    env["DISCORD_CHANNEL_IDS"] = ""

    merlin_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    env.pop("MERLIN_HOME", None)

    proc = subprocess.Popen(
        ["uv", "run", "main.py", "--port", str(port)],
        cwd=merlin_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    url = f"http://localhost:{port}"
    for _ in range(30):
        try:
            import urllib.request

            urllib.request.urlopen(f"{url}/api/files/browse?path=/tmp", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Server failed to start")

    yield url

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def browser_context(server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1200, "height": 800})
        yield ctx, server
        ctx.close()
        browser.close()


def _open_pdf(ctx, url, file_path, viewport=None):
    """Open a PDF with ?test=1 and wait until it mounts and a page renders."""
    page = ctx.new_page()
    if viewport:
        page.set_viewport_size(viewport)
    page.goto(f"{url}/files{file_path}?test=1", wait_until="networkidle")
    page.wait_for_selector(".pdf-preview", timeout=15000)
    page.wait_for_function(
        "() => window.__merlinPdfTest && window.__merlinPdfTest.numPages > 0",
        timeout=15000,
    )
    page.wait_for_function(
        "() => window.__merlinPdfTest && window.__merlinPdfTest.renderedPageCount > 0",
        timeout=15000,
    )
    return page


# ---------------------------------------------------------------------------
# Mount + page count
# ---------------------------------------------------------------------------


class TestPdfMount:
    def test_pdf_mounts_and_reports_page_count(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_pdf(ctx, url, str(test_files / "sample_2page.pdf"))
        assert page.query_selector(".pdf-preview") is not None
        assert page.evaluate("() => window.__merlinPdfTest.numPages") == 2
        # One placeholder per page exists
        assert page.eval_on_selector_all(".pdf-page", "els => els.length") == 2
        page.close()

    def test_mobile_viewport_layout(self, browser_context, test_files):
        """Counter pill visible inside the preview wrapper, no header overlap."""
        ctx, url = browser_context
        page = _open_pdf(
            ctx,
            url,
            str(test_files / "sample_2page.pdf"),
            viewport={"width": 375, "height": 667},
        )
        box = page.evaluate("""() => {
            const pill = document.querySelector('.pdf-counter');
            const wrap = document.querySelector('.pdf-preview');
            if (!pill || !wrap) return null;
            const p = pill.getBoundingClientRect();
            const w = wrap.getBoundingClientRect();
            return {
                visible: p.width > 0 && p.height > 0,
                inside: p.top >= w.top - 1 && p.right <= w.right + 1,
            };
        }""")
        assert box is not None
        assert box["visible"] is True
        assert box["inside"] is True

        header_bottom = page.evaluate(
            "() => document.querySelector('#file-header').getBoundingClientRect().bottom"
        )
        preview_top = page.evaluate(
            "() => document.querySelector('.pdf-preview').getBoundingClientRect().top"
        )
        assert preview_top >= header_bottom - 1
        page.close()


# ---------------------------------------------------------------------------
# Rendering actually happens (not a blank canvas)
# ---------------------------------------------------------------------------


class TestPdfRenderingActuallyHappens:
    def test_first_page_canvas_is_not_blank(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_pdf(ctx, url, str(test_files / "sample_2page.pdf"))
        page.wait_for_selector(".pdf-page canvas", timeout=15000)
        # The fixture draws a black rectangle on white -> high pixel variance.
        variance = page.evaluate("""() => {
            const canvas = document.querySelector('.pdf-page canvas');
            if (!canvas) return -1;
            const g = canvas.getContext('2d');
            const { width, height } = canvas;
            const data = g.getImageData(0, 0, width, height).data;
            let min = 255, max = 0;
            // Sample the red channel across the image
            for (let i = 0; i < data.length; i += 400) {
                const v = data[i];
                if (v < min) min = v;
                if (v > max) max = v;
            }
            return max - min;
        }""")
        assert variance > 50, f"canvas looks blank (range {variance})"
        page.close()


# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------


class TestPdfZoom:
    def test_zoom_in_button_increases_scale(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_pdf(ctx, url, str(test_files / "sample_2page.pdf"))
        before = page.evaluate("() => window.__merlinPdfTest.scale")
        before_zoom = page.evaluate("() => window.__merlinPdfTest.zoomFactor")
        page.click(".pdf-zoom button:last-child")  # the "+" button
        page.wait_for_function(
            "(z) => window.__merlinPdfTest.zoomFactor > z",
            arg=before_zoom,
            timeout=5000,
        )
        after = page.evaluate("() => window.__merlinPdfTest.scale")
        assert after > before
        page.close()

    def test_zoom_resizes_rendered_page_synchronously(
        self, browser_context, test_files
    ):
        """Regression: on zoom, an already-rendered page's on-screen width must
        grow in the same turn (before the debounced sharp re-render). If it only
        grew after the debounce, the page would snap back to the old size for a
        few frames on pinch release."""
        ctx, url = browser_context
        page = _open_pdf(ctx, url, str(test_files / "sample_2page.pdf"))
        page.wait_for_selector(".pdf-page canvas", timeout=15000)
        before_w = page.evaluate(
            "() => document.querySelector('.pdf-page').getBoundingClientRect().width"
        )
        # Bump the zoom and read the width back immediately, without waiting for
        # the ~150ms re-render debounce.
        new_w = page.evaluate("""() => {
            document.querySelector('.pdf-zoom button:last-child').click();
            return document.querySelector('.pdf-page').getBoundingClientRect().width;
        }""")
        assert new_w > before_w + 1, (
            f"rendered page did not resize synchronously ({before_w} -> {new_w})"
        )
        page.close()

    def test_zoom_keeps_viewport_center_anchored(self, browser_context, test_files):
        """Regression: zooming must keep the vertical center of the viewport
        anchored, not let the document drift/scroll away. From a mid-document
        scroll position, the center-of-viewport fraction should stay roughly
        constant across a zoom (and must not jump back to the top)."""
        ctx, url = browser_context
        page = _open_pdf(ctx, url, str(test_files / "sample_2page.pdf"))
        page.wait_for_selector(".pdf-page canvas", timeout=15000)

        # Scroll to the middle of the document.
        page.eval_on_selector(
            ".pdf-scroll", "el => el.scrollTop = (el.scrollHeight - el.clientHeight) / 2"
        )
        before = page.evaluate("""() => {
            const el = document.querySelector('.pdf-scroll');
            return (el.scrollTop + el.clientHeight / 2) / el.scrollHeight;
        }""")
        # Zoom in.
        page.click(".pdf-zoom button:last-child")
        after = page.evaluate("""() => {
            const el = document.querySelector('.pdf-scroll');
            return {
                ratio: (el.scrollTop + el.clientHeight / 2) / el.scrollHeight,
                scrollTop: el.scrollTop,
            };
        }""")
        assert after["scrollTop"] > 5, "zoom jumped the document back to the top"
        assert abs(after["ratio"] - before) < 0.05, (
            f"viewport center drifted on zoom ({before:.3f} -> {after['ratio']:.3f})"
        )
        page.close()


# ---------------------------------------------------------------------------
# Bad-file fallback
# ---------------------------------------------------------------------------


class TestPdfBadFileFallback:
    def test_corrupt_pdf_falls_back_to_binary_info(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(
            f"{url}/files{test_files / 'broken.pdf'}?test=1",
            wait_until="networkidle",
        )
        page.wait_for_selector(".binary-info", timeout=15000)
        # PDF preview must NOT be present, and a reason banner should show
        assert page.query_selector(".pdf-preview") is None
        assert page.query_selector(".binary-message") is not None
        page.close()


# ---------------------------------------------------------------------------
# Dispose on navigation (no leaked document/worker/canvas)
# ---------------------------------------------------------------------------


class TestPdfDispose:
    def test_dispose_on_in_app_navigation(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_pdf(ctx, url, str(test_files / "sample_2page.pdf"))
        assert page.evaluate("() => window.__merlinPdfTest !== null") is True

        # zzz.txt sorts after sample_2page.pdf, so the next-sibling button
        # navigates to it and should tear down the PDF context.
        page.wait_for_selector("#file-next-btn:not([disabled])", timeout=5000)
        page.click("#file-next-btn")
        page.wait_for_function(
            "() => !document.querySelector('.pdf-preview')",
            timeout=5000,
        )
        assert page.evaluate("() => window.__merlinPdfTest === null") is True
        page.close()
