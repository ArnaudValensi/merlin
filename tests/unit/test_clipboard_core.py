"""Guards on the shared clipboard partial (terminal/templates/clipboard-core.js).

The clipboard logic used to exist twice: once in the terminal, once in the
/clipboard-test diagnostic page. They drifted, and the page started reporting
success on paths where the terminal reported "Clipboard blocked" — which is
how the iOS paste bug stayed invisible. These tests keep the single-source
arrangement from quietly coming apart again.

The behaviour of the ladder itself is covered by tests/js/clipboard-core.test.js.
"""

import re

import pytest

from terminal import routes as tr

CORE_JS = tr.TERMINAL_TEMPLATES_DIR / "clipboard-core.js"
INCLUDE = '{% include "clipboard-core.js" %}'


def test_core_partial_exists():
    assert CORE_JS.is_file()


def test_core_partial_is_free_of_jinja_delimiters():
    """It is rendered as a template, so a stray delimiter is a 500 at runtime."""
    source = CORE_JS.read_text()
    for delimiter in ("{{", "{%", "{#"):
        assert delimiter not in source, f"{delimiter} in clipboard-core.js"


# The terminal pages (classic and native) get it through their shared chrome.
CHROME = '{% include "terminal-chrome.html" %}'
NATIVE_PAGE_JS = tr.TERMINAL_TEMPLATES_DIR.parent / "static" / "native" / "page.js"


@pytest.mark.parametrize("template", ["terminal-chrome.html", "clipboard-test.html"])
def test_every_page_includes_the_shared_core(template):
    """No page may grow its own copy of the paste ladder."""
    source = (tr.TERMINAL_TEMPLATES_DIR / template).read_text()
    assert INCLUDE in source


@pytest.mark.parametrize("template", ["terminal.html", "terminal-native.html"])
def test_both_terminal_pages_include_the_shared_chrome(template):
    source = (tr.TERMINAL_TEMPLATES_DIR / template).read_text()
    assert CHROME in source
    assert INCLUDE not in source


@pytest.mark.parametrize(
    "path",
    [
        tr.TERMINAL_TEMPLATES_DIR / "terminal.html",
        tr.TERMINAL_TEMPLATES_DIR / "terminal-native.html",
        NATIVE_PAGE_JS,
    ],
    ids=lambda p: p.name,
)
def test_terminal_does_not_walk_clipboard_items_itself(path):
    """navigator.clipboard.read() belongs to the shared core, nowhere else.

    readText() is exempt: the permission icon deliberately probes it directly
    to find out whether Chrome has granted clipboard-read.
    """
    assert not re.search(r"navigator\.clipboard\.read\(", path.read_text())


def test_diagnostic_page_renders_with_the_core_inlined():
    """Renders for real, so a broken or renamed include fails here, not in prod."""
    html = tr.templates.env.get_template("clipboard-test.html").render()
    assert "MerlinClipboard" in html
    assert "readClipboard" in html
