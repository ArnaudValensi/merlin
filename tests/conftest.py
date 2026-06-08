import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_project_root = str(Path(__file__).parent.parent)

# Add project root to sys.path so tests can import core modules
sys.path.insert(0, _project_root)

# Add lib/ so `from lib.claude import ...` works
sys.path.insert(0, str(Path(_project_root) / "lib"))

# Also add merlin-bot/ for tests that reference bot modules (transcribe, etc.)
sys.path.insert(0, str(Path(_project_root) / "merlin-bot"))

# ---------------------------------------------------------------------------
# Test isolation: redirect MERLIN_HOME BEFORE any test modules are imported.
#
# Many modules cache paths at import time (e.g. structured_log.ENGINE_LOG_PATH).
# Setting the env var here — at conftest load time — ensures those module-level
# constants resolve to a temp dir instead of ~/.merlin/. Without this, tests
# write fake crash events to the production engine-log.jsonl and the live
# Merlin process sends spurious Discord notifications.
# ---------------------------------------------------------------------------
_session_merlin_home = tempfile.mkdtemp(prefix="merlin-test-")
os.environ["MERLIN_HOME"] = _session_merlin_home

# Clean up stale temp dirs from interrupted runs (Ctrl+C, segfault, etc.)
_tmp = Path(tempfile.gettempdir())
for _stale in _tmp.glob("merlin-test-*"):
    if _stale != Path(_session_merlin_home) and _stale.is_dir():
        shutil.rmtree(_stale, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_merlin_home(monkeypatch, tmp_path):
    """Point MERLIN_HOME to a per-test temp dir so no test touches ~/.merlin/.

    Also clear any ambient MERLIN_DEV so dev-mode detection is deterministic:
    with the override reset and the env var gone, is_dev_mode() falls back to
    the repo's .git presence (dev mode), so app_dir() resolves to the repo.
    Tests that need a specific mode set it explicitly (MERLIN_DEV in the test
    body, or paths.set_dev_mode), which still wins over this default.
    """
    import paths

    monkeypatch.setenv("MERLIN_HOME", str(tmp_path))
    monkeypatch.delenv("MERLIN_DEV", raising=False)
    paths._dev_mode_override = None
    yield
    paths._dev_mode_override = None


def pytest_unconfigure(config):
    """Remove the session-wide temp MERLIN_HOME created at import time."""
    shutil.rmtree(_session_merlin_home, ignore_errors=True)
