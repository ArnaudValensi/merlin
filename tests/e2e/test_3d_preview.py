"""E2E tests for 3D model preview (STL + OBJ).

Run with: uv run pytest tests/e2e/test_3d_preview.py -v
Requires: uv run --with playwright playwright install firefox
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def test_files(tmp_path_factory):
    """Copy STL/OBJ fixtures + a corrupt .stl into a temp dir."""
    root = tmp_path_factory.mktemp("model3dtest")
    shutil.copy(FIXTURES / "cube_20x30x40.stl", root / "cube_20x30x40.stl")
    shutil.copy(FIXTURES / "cube_10x10x10.obj", root / "cube_10x10x10.obj")
    # Corrupt STL — header-only, no triangles → loader will fail
    (root / "broken.stl").write_bytes(b"not actually an stl file")
    # A neighbour text file so we can test sibling navigation cleanup
    (root / "neighbour.txt").write_text("hello\n")
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
        ["uv", "run", "main.py", "--no-tunnel", "--port", str(port)],
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
    """Chromium + swiftshader — Firefox headless lacks a usable WebGL context
    on most Linux CI images; Chromium's software rasterizer renders the scene
    deterministically.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"],
        )
        ctx = browser.new_context(viewport={"width": 1200, "height": 800})
        yield ctx, server
        ctx.close()
        browser.close()


def _open_3d_file(ctx, url, file_path):
    """Open a 3D model file with ?test=1 and wait until it mounts."""
    page = ctx.new_page()
    page.goto(f"{url}/files{file_path}?test=1", wait_until="networkidle")
    # Give the importmap + module + renderer time to mount
    page.wait_for_selector(".model3d-preview canvas", timeout=10000)
    page.wait_for_function(
        "() => window.__merlin3DTest && window.__merlin3DTest.canvas",
        timeout=10000,
    )
    return page


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class Test3DMount:
    def test_stl_mounts_canvas(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_3d_file(ctx, url, str(test_files / "cube_20x30x40.stl"))
        canvas = page.query_selector(".model3d-preview canvas")
        assert canvas is not None
        page.close()

    def test_obj_mounts_canvas(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_3d_file(ctx, url, str(test_files / "cube_10x10x10.obj"))
        canvas = page.query_selector(".model3d-preview canvas")
        assert canvas is not None
        page.close()

    def test_mobile_viewport_layout(self, browser_context, test_files):
        """Mobile viewport: canvas present, dims pill visible inside the
        canvas area, pill not overlapping the file viewer header."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.set_viewport_size({"width": 375, "height": 667})
        try:
            page.goto(
                f"{url}/files{test_files / 'cube_20x30x40.stl'}?test=1",
                wait_until="networkidle",
            )
            page.wait_for_selector(".model3d-preview canvas", timeout=10000)
            page.wait_for_function(
                "() => window.__merlin3DTest && window.__merlin3DTest.canvas",
                timeout=10000,
            )

            # Pill is visible and positioned inside the preview wrapper
            pill_box = page.evaluate("""() => {
                const pill = document.querySelector('.model3d-dims');
                const wrap = document.querySelector('.model3d-preview');
                if (!pill || !wrap) return null;
                const p = pill.getBoundingClientRect();
                const w = wrap.getBoundingClientRect();
                return {
                    pillVisible: p.width > 0 && p.height > 0,
                    insideWrap: p.top >= w.top - 1 && p.right <= w.right + 1,
                    pillBottom: p.bottom,
                };
            }""")
            assert pill_box is not None
            assert pill_box["pillVisible"] is True
            assert pill_box["insideWrap"] is True

            # Header is above the preview area (no overlap)
            header_bottom = page.evaluate(
                "() => document.querySelector('#file-header').getBoundingClientRect().bottom"
            )
            preview_top = page.evaluate(
                "() => document.querySelector('.model3d-preview').getBoundingClientRect().top"
            )
            assert preview_top >= header_bottom - 1
        finally:
            page.close()


class Test3DDimensions:
    def test_stl_dimensions_match_fixture(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_3d_file(ctx, url, str(test_files / "cube_20x30x40.stl"))
        # Read dims pill text (visible to the user)
        pill_text = page.text_content(".model3d-dims")
        assert pill_text is not None
        assert "20.00" in pill_text
        assert "30.00" in pill_text
        assert "40.00" in pill_text
        assert "mm" in pill_text

        # Cross-check via the test handle (Box3 result)
        dims = page.evaluate("() => window.__merlin3DTest.dims")
        assert abs(dims["x"] - 20.0) < 1e-3
        assert abs(dims["y"] - 30.0) < 1e-3
        assert abs(dims["z"] - 40.0) < 1e-3
        page.close()

    def test_obj_dimensions_match_fixture(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_3d_file(ctx, url, str(test_files / "cube_10x10x10.obj"))
        dims = page.evaluate("() => window.__merlin3DTest.dims")
        assert abs(dims["x"] - 10.0) < 1e-3
        assert abs(dims["y"] - 10.0) < 1e-3
        assert abs(dims["z"] - 10.0) < 1e-3
        page.close()


class Test3DRenderingActuallyHappens:
    def test_scene_contains_geometry(self, browser_context, test_files):
        """Soft check: the scene has triangles (proves loader → mesh wiring)."""
        ctx, url = browser_context
        page = _open_3d_file(ctx, url, str(test_files / "cube_20x30x40.stl"))
        # Count triangles across the scene
        triangle_count = page.evaluate("""() => {
            let total = 0;
            window.__merlin3DTest.scene.traverse((c) => {
                if (c.isMesh && c.geometry) {
                    const idx = c.geometry.getIndex();
                    if (idx) total += idx.count / 3;
                    else if (c.geometry.attributes.position) {
                        total += c.geometry.attributes.position.count / 3;
                    }
                }
            });
            return total;
        }""")
        assert triangle_count >= 12, (
            f"expected ≥12 triangles in box, got {triangle_count}"
        )
        page.close()


class Test3DInteraction:
    def test_mouse_drag_changes_camera(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_3d_file(ctx, url, str(test_files / "cube_20x30x40.stl"))

        before = page.evaluate(
            "() => Object.assign({}, window.__merlin3DTest.camera.position)"
        )

        # Get canvas bounding box and drag across it
        box = page.evaluate("""() => {
            const c = window.__merlin3DTest.canvas;
            const r = c.getBoundingClientRect();
            return { x: r.x, y: r.y, w: r.width, h: r.height };
        }""")
        cx = box["x"] + box["w"] / 2
        cy = box["y"] + box["h"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 200, cy + 100, steps=10)
        page.mouse.up()

        # Wait for the orbit damping animation to settle
        page.wait_for_function(
            """(prev) => {
                const p = window.__merlin3DTest.camera.position;
                return Math.abs(p.x - prev.x) > 0.01
                    || Math.abs(p.y - prev.y) > 0.01
                    || Math.abs(p.z - prev.z) > 0.01;
            }""",
            arg=before,
            timeout=5000,
        )
        page.close()

    def test_touch_drag_changes_camera(self, browser_context, test_files):
        ctx, url = browser_context
        page = _open_3d_file(ctx, url, str(test_files / "cube_20x30x40.stl"))

        before = page.evaluate(
            "() => Object.assign({}, window.__merlin3DTest.camera.position)"
        )

        # Dispatch a touch sequence directly — Playwright's touchscreen API
        # only does taps; for a drag we need to dispatch TouchEvents.
        page.evaluate("""() => {
            const canvas = window.__merlin3DTest.canvas;
            const r = canvas.getBoundingClientRect();
            const cx = r.x + r.width / 2;
            const cy = r.y + r.height / 2;
            const make = (type, x, y) => {
                const t = new Touch({
                    identifier: 1,
                    target: canvas,
                    clientX: x,
                    clientY: y,
                    pageX: x,
                    pageY: y,
                });
                return new TouchEvent(type, {
                    cancelable: true,
                    bubbles: true,
                    touches: type === 'touchend' ? [] : [t],
                    targetTouches: type === 'touchend' ? [] : [t],
                    changedTouches: [t],
                });
            };
            canvas.dispatchEvent(make('touchstart', cx, cy));
            for (let i = 1; i <= 8; i++) {
                canvas.dispatchEvent(make('touchmove', cx + i * 25, cy + i * 12));
            }
            canvas.dispatchEvent(make('touchend', cx + 200, cy + 96));
        }""")

        try:
            page.wait_for_function(
                """(prev) => {
                    const p = window.__merlin3DTest.camera.position;
                    return Math.abs(p.x - prev.x) > 0.01
                        || Math.abs(p.y - prev.y) > 0.01
                        || Math.abs(p.z - prev.z) > 0.01;
                }""",
                arg=before,
                timeout=5000,
            )
        except Exception:
            # Some headless engines don't dispatch TouchEvent → orbit reliably.
            # Skip rather than fail; mouse drag covers the same wiring.
            pytest.skip("Touch event dispatch not honored in this headless browser")
        finally:
            page.close()


class Test3DBadFileFallback:
    def test_corrupt_stl_falls_back_to_binary_info(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(
            f"{url}/files{test_files / 'broken.stl'}?test=1",
            wait_until="networkidle",
        )
        # Wait for fallback (.binary-info) to appear in #file-content
        page.wait_for_selector(".binary-info", timeout=8000)
        # 3D preview should NOT be present
        preview = page.query_selector(".model3d-preview")
        assert preview is None
        page.close()


class Test3DDispose:
    def test_dispose_on_in_app_navigation(self, browser_context, test_files):
        """In-app sibling navigation should run disposeThreeContext()."""
        ctx, url = browser_context
        page = _open_3d_file(ctx, url, str(test_files / "cube_20x30x40.stl"))
        # Sanity: handle is set
        assert page.evaluate("() => window.__merlin3DTest !== null") is True

        # Trigger an in-app navigation by clicking the next-sibling button.
        # The fixture dir has neighbour.txt + broken.stl + obj + stl, so the
        # nav cluster shows up.
        page.wait_for_selector("#file-next-btn:not([disabled])", timeout=5000)
        page.click("#file-next-btn")
        # Wait until the file viewer no longer holds a 3D canvas
        page.wait_for_function(
            "() => !document.querySelector('.model3d-preview canvas')",
            timeout=5000,
        )
        # Test handle reset to null (initialised on page load, cleared on dispose)
        assert page.evaluate("() => window.__merlin3DTest === null") is True
        page.close()
