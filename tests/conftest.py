import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_project_root = str(Path(__file__).parent.parent)

# Add project root to sys.path so tests can import core modules
sys.path.insert(0, _project_root)

# Add lib/ so `from lib.skills import ...` works
sys.path.insert(0, str(Path(_project_root) / "lib"))

# ---------------------------------------------------------------------------
# Test isolation: redirect MERLIN_HOME BEFORE any test modules are imported.
#
# Many modules cache paths at import time (e.g. structured_log.EVENT_LOG_PATH).
# Setting the env var here — at conftest load time — ensures those module-level
# constants resolve to a temp dir instead of ~/.merlin/. Without this, tests
# write fake crash events to the production events.jsonl.
# ---------------------------------------------------------------------------
_session_merlin_home = tempfile.mkdtemp(prefix="merlin-test-")
os.environ["MERLIN_HOME"] = _session_merlin_home
# The suite imports main with the Timeline flag on and the notes extension
# enabled, so their routes exist for the suites that exercise them; an
# ambient MERLIN_FEATURES (exported by a shell that sourced config.env)
# must not leak in. Tests of the flag off, or of notes off by default,
# set the environment and a state file of their own.
os.environ["MERLIN_FEATURES"] = "timeline"
with open(os.path.join(_session_merlin_home, "extensions.json"), "w") as _state:
    json.dump({"notes": True}, _state)

# tmux too: Merlin's bare ``tmux`` calls (a job run opening its window, the
# board, the workspace sweep) must never reach the developer's own server.
# TMUX_TMPDIR points tmux's default socket at a private directory for the
# whole session, an inherited $TMUX would make a spawned server refuse to
# nest, and a guarding ``tmux`` shim first on PATH refuses any call that
# could fall back to the real server (see tests/tmux_isolation.py). The
# server a test leaves on the session socket is killed, by that socket, at
# the end of the session. Real-tmux suites set their own socket on top.
sys.path.insert(0, str(Path(__file__).parent))
import tmux_isolation  # noqa: E402

_session_tmux_dir = tmux_isolation.new_session_dir()
os.environ["TMUX_TMPDIR"] = str(_session_tmux_dir)
os.environ.pop("TMUX", None)
os.environ.pop("TMUX_PANE", None)
tmux_isolation.install_guard(_session_tmux_dir)

# Clean up stale temp dirs from interrupted runs (Ctrl+C, segfault, etc.),
# only once they are hours old: another pytest run may be using a younger one
# right now (an e2e run beside a unit run), and deleting a live run's socket
# directory is exactly what sends its teardown to the real server.
tmux_isolation.remove_stale("merlin-test-", Path(_session_merlin_home))
tmux_isolation.remove_stale("merlin-tmux-", _session_tmux_dir)


@pytest.fixture(autouse=True)
def _isolated_merlin_home(monkeypatch, tmp_path):
    """Point MERLIN_HOME to a per-test temp dir so no test touches ~/.merlin/.

    Also clear any ambient MERLIN_DEV so dev-mode detection is deterministic:
    with the override reset and the env var gone, is_dev_mode() falls back to
    the repo's .git presence (dev mode), so app_dir() resolves to the repo.
    Tests that need a specific mode set it explicitly (MERLIN_DEV in the test
    body, or paths.set_dev_mode), which still wins over this default.
    MERLIN_FEATURES holds the Timeline flag alone, as at import: the app
    flag starts off, and a test of a flag off deletes the variable.
    """
    import paths

    monkeypatch.setenv("MERLIN_HOME", str(tmp_path))
    monkeypatch.delenv("MERLIN_DEV", raising=False)
    monkeypatch.setenv("MERLIN_FEATURES", "timeline")
    # The voice provider follows these (voice/tts.py): an ambient one, from a
    # shell that sourced config.env, would pick a provider a test did not set.
    for key in list(os.environ):
        if key.startswith("MERLIN_TTS_") or key in (
            "MERLIN_SAAS_TOKEN",
            "MERLIN_SAAS_API",
        ):
            monkeypatch.delenv(key)
    paths._dev_mode_override = None
    yield
    paths._dev_mode_override = None


def pytest_unconfigure(config):
    """Remove the session-wide temp MERLIN_HOME and tmux socket directory
    created at import time, killing the private server a test may have
    started there: by its socket, never through an environment variable."""
    shutil.rmtree(_session_merlin_home, ignore_errors=True)
    tmux_isolation.kill_server(_session_tmux_dir)
    shutil.rmtree(_session_tmux_dir, ignore_errors=True)
