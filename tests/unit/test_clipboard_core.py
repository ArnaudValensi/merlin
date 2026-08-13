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


@pytest.mark.parametrize("template", ["terminal.html", "clipboard-test.html"])
def test_both_pages_include_the_shared_core(template):
    """Neither page may grow its own copy of the paste ladder."""
    source = (tr.TERMINAL_TEMPLATES_DIR / template).read_text()
    assert INCLUDE in source


def test_terminal_does_not_walk_clipboard_items_itself():
    """navigator.clipboard.read() belongs to the shared core, nowhere else.

    readText() is exempt: the permission icon deliberately probes it directly
    to find out whether Chrome has granted clipboard-read.
    """
    source = (tr.TERMINAL_TEMPLATES_DIR / "terminal.html").read_text()
    assert not re.search(r"navigator\.clipboard\.read\(", source)


def test_diagnostic_page_renders_with_the_core_inlined():
    """Renders for real, so a broken or renamed include fails here, not in prod."""
    html = tr.templates.env.get_template("clipboard-test.html").render()
    assert "MerlinClipboard" in html
    assert "readClipboard" in html
