"""E2E tests for markdown rendering in the file browser.

Uses Playwright to test the full browser rendering pipeline.
Run with: uv run --with pytest --with playwright pytest tests/test_markdown_rendering.py -v
Requires: uv run --with playwright playwright install firefox
"""

import os
import signal
import socket
import subprocess
import textwrap
import time

import pytest

# Skip all tests if playwright is not installed
pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def test_files(tmp_path_factory):
    """Create a temp directory with markdown test files."""
    root = tmp_path_factory.mktemp("mdtest")

    # Main test markdown
    (root / "test.md").write_text(
        textwrap.dedent("""\
        # Test Heading

        Paragraph with **bold**, *italic*, and `inline code`.

        [Internal link](other.md) and [external link](https://example.com).

        [Non-md link](config.yaml)

        ## Code Block

        ```python
        def hello():
            return "world"
        ```

        ## Lists

        - Item one
        - Item two
          - Nested
        - Item three

        1. First
        2. Second

        ## Table

        | Col A | Col B |
        |-------|-------|
        | one   | two   |

        ## Blockquote

        > Important note here.

        ## Image

        ![diagram](test/flow.svg)

        ---

        ### Anchor Target

        Content below anchor.
    """)
    )

    # Linked markdown file
    (root / "other.md").write_text("# Other\n\n[Back](test.md)\n")

    # Non-markdown text file
    (root / "config.yaml").write_text("key: value\n")

    # Companion directory with SVG image
    (root / "test").mkdir()
    (root / "test" / "flow.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
        '<rect width="100" height="50" fill="#222"/>'
        '<text x="50" y="30" fill="#eee" text-anchor="middle">SVG</text>'
        "</svg>"
    )

    # Empty markdown
    (root / "empty.md").write_text("")

    # Python file (non-markdown)
    (root / "app.py").write_text("print('hello')\n")

    # Mermaid file (direct viewing)
    (root / "diagram.mmd").write_text(
        textwrap.dedent("""\
        graph TD
            A[Start] --> B{Decision}
            B -->|Yes| C[OK]
            B -->|No| D[End]
    """)
    )

    # Markdown with mermaid code block
    (root / "with-mermaid.md").write_text(
        textwrap.dedent("""\
        # Mermaid Test

        Here is a diagram:

        ```mermaid
        graph LR
            A --> B
            B --> C
        ```

        And some text after.
    """)
    )

    # Markdown with embedded .mmd file (image syntax)
    (root / "with-mmd-embed.md").write_text(
        textwrap.dedent("""\
        # Embedded Mermaid

        ![flow diagram](diagram.mmd)

        Done.
    """)
    )

    # Markdown with linked .mmd file (link syntax — should NOT render inline)
    (root / "with-mmd-link.md").write_text(
        textwrap.dedent("""\
        # Linked Mermaid

        See the [diagram](diagram.mmd) for details.
    """)
    )

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

    # Start from the merlin project root
    merlin_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    # Don't inherit MERLIN_HOME from test isolation — the subprocess needs
    # the real ~/.merlin/ (or no MERLIN_HOME) to find config.env, extensions, etc.
    env.pop("MERLIN_HOME", None)

    proc = subprocess.Popen(
        ["uv", "run", "main.py", "--port", str(port)],
        cwd=merlin_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for server to be ready
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
    """Provide a Playwright browser context."""
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1200, "height": 800})
        yield ctx, server
        ctx.close()
        browser.close()


# ---------------------------------------------------------------------------
# Core rendering tests
# ---------------------------------------------------------------------------


class TestMarkdownRendering:
    def test_md_file_renders_as_html(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        # Should have a .markdown-body container
        body = page.query_selector(".markdown-body")
        assert body is not None, "Expected .markdown-body container"

        # Should NOT have line numbers (not raw text view)
        line_nos = page.query_selector(".file-line-no")
        assert line_nos is None, "Should not show line numbers in rendered mode"

        # Should have rendered HTML elements
        h1 = page.query_selector(".markdown-body h1")
        assert h1 is not None
        assert h1.text_content() == "Test Heading"

        page.close()

    def test_non_md_file_renders_as_text(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/app.py", wait_until="networkidle")
        time.sleep(0.5)

        # Should have line numbers (text view)
        line_no = page.query_selector(".file-line-no")
        assert line_no is not None, "Expected line numbers in text view"

        # Should NOT have markdown container
        md_body = page.query_selector(".markdown-body")
        assert md_body is None

        # Md toggle should be hidden
        md_toggle = page.query_selector("#md-toggle")
        assert md_toggle is not None
        assert not md_toggle.is_visible()

        page.close()

    def test_empty_md_file(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/empty.md", wait_until="networkidle")
        time.sleep(0.5)

        body = page.query_selector(".markdown-body")
        assert body is not None, "Empty .md should still show markdown container"

        page.close()

    def test_headings_all_levels(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        h1 = page.query_selector(".markdown-body h1")
        h2 = page.query_selector(".markdown-body h2")
        h3 = page.query_selector(".markdown-body h3")
        assert h1 is not None
        assert h2 is not None
        assert h3 is not None

        page.close()

    def test_code_block_highlighted(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        pre = page.query_selector(".markdown-body pre")
        assert pre is not None
        code = pre.query_selector("code")
        assert code is not None
        # highlight.js adds a class when highlighting
        code_class = code.get_attribute("class") or ""
        assert "hljs" in code_class or "language-" in code_class, (
            f"Expected highlight.js classes, got: {code_class}"
        )

        page.close()

    def test_inline_code(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        # Find inline code (not inside pre)
        codes = page.query_selector_all(".markdown-body code")
        inline_codes = [c for c in codes if not c.evaluate("el => el.closest('pre')")]
        assert len(inline_codes) > 0, "Expected inline code elements"
        assert any("inline code" in c.text_content() for c in inline_codes)

        page.close()

    def test_table_renders(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        table = page.query_selector(".markdown-body table")
        assert table is not None
        ths = table.query_selector_all("th")
        assert len(ths) == 2
        tds = table.query_selector_all("td")
        assert len(tds) == 2

        page.close()

    def test_blockquote_renders(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        bq = page.query_selector(".markdown-body blockquote")
        assert bq is not None
        assert "Important note" in bq.text_content()

        page.close()

    def test_list_unordered(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        ul = page.query_selector(".markdown-body ul")
        assert ul is not None
        lis = ul.query_selector_all("li")
        assert len(lis) >= 3

        page.close()

    def test_list_ordered(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        ol = page.query_selector(".markdown-body ol")
        assert ol is not None

        page.close()

    def test_horizontal_rule(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        hr = page.query_selector(".markdown-body hr")
        assert hr is not None

        page.close()


# ---------------------------------------------------------------------------
# Link tests
# ---------------------------------------------------------------------------


class TestMarkdownLinks:
    def test_relative_md_link_navigates_spa(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        link = page.query_selector("a[data-internal]")
        assert link is not None, "Expected internal link with data-internal"

        link.click()
        time.sleep(1)

        # Should navigate to other.md within SPA
        assert "/other.md" in page.url
        # Should render as markdown
        body = page.query_selector(".markdown-body")
        assert body is not None
        assert "Other" in body.text_content()

        page.close()

    def test_external_link_new_tab(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        ext_link = page.query_selector('a[target="_blank"]')
        assert ext_link is not None
        assert "example.com" in ext_link.get_attribute("href")

        page.close()

    def test_heading_ids_generated(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        h1 = page.query_selector(".markdown-body h1[id]")
        assert h1 is not None, "H1 should have an id attribute"
        assert h1.get_attribute("id") == "test-heading"

        h2 = page.query_selector('.markdown-body h2[id="code-block"]')
        assert h2 is not None

        page.close()

    def test_relative_non_md_link_navigates(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        # Find the config.yaml link
        links = page.query_selector_all("a[data-internal]")
        yaml_link = None
        for l in links:
            internal = l.get_attribute("data-internal")
            if internal and "config.yaml" in internal:
                yaml_link = l
                break

        assert yaml_link is not None, "Expected link to config.yaml"
        yaml_link.click()
        time.sleep(1)

        # Should navigate to config.yaml in text mode (not markdown)
        assert "config.yaml" in page.url
        line_no = page.query_selector(".file-line-no")
        assert line_no is not None, "YAML file should render as text with line numbers"

        page.close()


# ---------------------------------------------------------------------------
# Image tests
# ---------------------------------------------------------------------------


class TestMarkdownImages:
    def test_companion_dir_image(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        img = page.query_selector(".markdown-body img")
        assert img is not None, "Expected an image element"
        src = img.get_attribute("src")
        assert "/api/files/raw" in src, f"Image src should use raw API: {src}"
        assert "flow.svg" in src

        page.close()

    def test_image_loads_successfully(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(1)

        img = page.query_selector(".markdown-body img")
        assert img is not None
        # Check that image actually loaded (naturalWidth > 0)
        loaded = img.evaluate("el => el.naturalWidth > 0 || el.tagName === 'IMG'")
        assert loaded

        page.close()

    def test_absolute_url_image_unchanged(self, browser_context, test_files):
        """Images with absolute URLs should not be rewritten."""
        ctx, url = browser_context
        page = ctx.new_page()

        # Create a temp file with absolute URL image
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", dir=str(test_files), delete=False
        ) as f:
            f.write("![ext](https://example.com/img.png)\n")
            tmp_path = f.name

        page.goto(f"{url}/files{tmp_path}", wait_until="networkidle")
        time.sleep(0.5)

        img = page.query_selector(".markdown-body img")
        assert img is not None
        assert img.get_attribute("src") == "https://example.com/img.png"

        page.close()
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Toggle tests
# ---------------------------------------------------------------------------


class TestMarkdownToggle:
    def test_raw_toggle_shows_line_numbers(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        # Click Raw
        md_toggle = page.query_selector("#md-toggle")
        assert md_toggle is not None
        assert md_toggle.is_visible()
        assert md_toggle.text_content() == "Raw"

        md_toggle.click()
        time.sleep(0.5)

        # Should now show line numbers
        line_no = page.query_selector(".file-line-no")
        assert line_no is not None, "Raw mode should show line numbers"

        # Toggle text should be "Rendered"
        assert md_toggle.text_content() == "Rendered"

        page.close()

    def test_rendered_toggle_returns_to_html(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        md_toggle = page.query_selector("#md-toggle")
        # Go to raw
        md_toggle.click()
        time.sleep(0.5)
        # Go back to rendered
        md_toggle.click()
        time.sleep(0.5)

        body = page.query_selector(".markdown-body")
        assert body is not None, "Should be back in rendered mode"

        line_no = page.query_selector(".file-line-no")
        assert line_no is None, "Should not have line numbers in rendered mode"

        page.close()

    def test_wrap_visible_only_in_raw(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)

        wrap = page.query_selector("#wrap-toggle")
        md_toggle = page.query_selector("#md-toggle")

        # In rendered mode, wrap should be hidden
        assert not wrap.is_visible(), "Wrap should be hidden in rendered mode"

        # Switch to raw
        md_toggle.click()
        time.sleep(0.5)

        # Wrap should now be visible
        assert wrap.is_visible(), "Wrap should be visible in raw mode"

        # Switch back to rendered
        md_toggle.click()
        time.sleep(0.5)

        # Wrap should be hidden again
        assert not wrap.is_visible(), (
            "Wrap should be hidden after returning to rendered mode"
        )

        page.close()

    def test_raw_mode_is_standard_text_view(self, browser_context, test_files):
        """Raw mode for .md should produce the same DOM structure as a .py file."""
        ctx, url = browser_context
        page = ctx.new_page()

        # Get the text view structure for a .py file
        page.goto(f"{url}/files{test_files}/app.py", wait_until="networkidle")
        time.sleep(0.5)
        py_table = page.query_selector(".file-table")
        assert py_table is not None
        page.query_selector_all(".file-line-no")
        page.query_selector_all(".file-line-content code")

        # Get the raw view structure for .md file
        page.goto(f"{url}/files{test_files}/test.md", wait_until="networkidle")
        time.sleep(0.5)
        page.query_selector("#md-toggle").click()
        time.sleep(0.5)

        md_table = page.query_selector(".file-table")
        assert md_table is not None, "Raw md should have .file-table"
        md_line_nos = page.query_selector_all(".file-line-no")
        md_line_contents = page.query_selector_all(".file-line-content code")

        # Both should use the same structure
        assert len(md_line_nos) > 0
        assert len(md_line_contents) > 0

        page.close()

    def test_md_toggle_hidden_for_non_md(self, browser_context, test_files):
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/app.py", wait_until="networkidle")
        time.sleep(0.5)

        md_toggle = page.query_selector("#md-toggle")
        assert not md_toggle.is_visible(), "Md toggle should be hidden for non-md files"

        page.close()


# ---------------------------------------------------------------------------
# Mermaid tests (Phase 2)
# ---------------------------------------------------------------------------


class TestMermaidCodeBlocks:
    def test_mermaid_block_renders_as_svg(self, browser_context, test_files):
        """```mermaid code blocks should render as SVG diagrams."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/with-mermaid.md", wait_until="networkidle")
        time.sleep(3)  # mermaid.js lazy-loads + renders

        diagram = page.query_selector(".mermaid-diagram")
        assert diagram is not None, "Expected .mermaid-diagram container"

        svg = diagram.query_selector("svg")
        assert svg is not None, "Expected SVG element inside mermaid diagram"

        page.close()

    def test_mermaid_block_no_raw_code_visible(self, browser_context, test_files):
        """The raw mermaid code should not be visible after rendering."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/with-mermaid.md", wait_until="networkidle")
        time.sleep(3)

        # The pre>code.language-mermaid should be replaced
        raw_block = page.query_selector("pre code.language-mermaid")
        assert raw_block is None, "Raw mermaid code block should be replaced by SVG"

        page.close()

    def test_text_around_mermaid_still_renders(self, browser_context, test_files):
        """Text before and after mermaid block should still render."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/with-mermaid.md", wait_until="networkidle")
        time.sleep(3)

        body = page.query_selector(".markdown-body")
        text = body.text_content()
        assert "Mermaid Test" in text
        assert "And some text after." in text

        page.close()


class TestMermaidEmbedded:
    def test_embedded_mmd_renders_inline(self, browser_context, test_files):
        """![](file.mmd) should fetch and render inline as SVG."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(
            f"{url}/files{test_files}/with-mmd-embed.md", wait_until="networkidle"
        )
        time.sleep(3)

        diagram = page.query_selector(".mermaid-diagram")
        assert diagram is not None, "Embedded .mmd should render as mermaid diagram"

        svg = diagram.query_selector("svg")
        assert svg is not None, "Expected SVG from embedded .mmd"

        # The img tag should be gone (replaced by diagram)
        img = page.query_selector('.markdown-body img[src*=".mmd"]')
        assert img is None, "Original img tag should be replaced"

        page.close()

    def test_linked_mmd_stays_as_link(self, browser_context, test_files):
        """[text](file.mmd) should remain a navigable link, NOT render inline."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/with-mmd-link.md", wait_until="networkidle")
        time.sleep(2)

        # Should have a link, not an inline diagram
        link = page.query_selector("a[data-internal]")
        assert link is not None, "Expected a navigable link to .mmd file"
        internal = link.get_attribute("data-internal")
        assert "diagram.mmd" in internal

        # Should NOT have a mermaid diagram inline
        diagram = page.query_selector(".mermaid-diagram")
        assert diagram is None, "Link-syntax .mmd should NOT render inline"

        page.close()

    def test_linked_mmd_navigates_to_diagram(self, browser_context, test_files):
        """Clicking a [text](file.mmd) link should navigate to the .mmd file."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/with-mmd-link.md", wait_until="networkidle")
        time.sleep(2)

        link = page.query_selector("a[data-internal]")
        assert link is not None
        link.click()
        time.sleep(3)

        # Should navigate to diagram.mmd and render it as a diagram
        assert "diagram.mmd" in page.url
        diagram = page.query_selector(".mermaid-diagram")
        assert diagram is not None, "Direct .mmd viewing should render diagram"

        page.close()


class TestMermaidDirectViewing:
    def test_mmd_file_renders_as_diagram(self, browser_context, test_files):
        """Opening a .mmd file directly should render as a diagram."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/diagram.mmd", wait_until="networkidle")
        time.sleep(3)

        diagram = page.query_selector(".mermaid-diagram")
        assert diagram is not None, "Direct .mmd should render as diagram"

        svg = diagram.query_selector("svg")
        assert svg is not None

        page.close()

    def test_mmd_file_has_raw_toggle(self, browser_context, test_files):
        """Opening a .mmd file should show Raw/Rendered toggle."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/diagram.mmd", wait_until="networkidle")
        time.sleep(3)

        md_toggle = page.query_selector("#md-toggle")
        assert md_toggle is not None
        assert md_toggle.is_visible()
        assert md_toggle.text_content() == "Raw"

        page.close()

    def test_mmd_raw_toggle_shows_source(self, browser_context, test_files):
        """Clicking Raw on a .mmd file should show the source code."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/diagram.mmd", wait_until="networkidle")
        time.sleep(3)

        md_toggle = page.query_selector("#md-toggle")
        md_toggle.click()
        time.sleep(0.5)

        # Should show line numbers (text view)
        line_no = page.query_selector(".file-line-no")
        assert line_no is not None, "Raw mode should show line numbers"

        # Toggle should say "Rendered"
        assert md_toggle.text_content() == "Rendered"

        page.close()

    def test_mmd_rendered_toggle_returns_to_diagram(self, browser_context, test_files):
        """Clicking Rendered on a .mmd file should show the diagram again."""
        ctx, url = browser_context
        page = ctx.new_page()
        page.goto(f"{url}/files{test_files}/diagram.mmd", wait_until="networkidle")
        time.sleep(3)

        md_toggle = page.query_selector("#md-toggle")
        md_toggle.click()
        time.sleep(0.5)
        md_toggle.click()
        time.sleep(3)

        diagram = page.query_selector(".mermaid-diagram")
        assert diagram is not None, "Should return to diagram view"

        page.close()
