import sys
from pathlib import Path

import pytest

# Add merlin-bot/ to sys.path so tests can import merlin_app, structured_log, etc.
sys.path.insert(0, str(Path(__file__).parent.parent))

# Add project root so merlin_app can import auth, paths, etc.
_project_root = str(Path(__file__).parent.parent.parent)
sys.path.insert(0, _project_root)

# Add lib/ so imports like `from lib.claude import ...` work
sys.path.insert(0, str(Path(_project_root) / "lib"))


@pytest.fixture(autouse=True)
def _isolated_merlin_home(monkeypatch, tmp_path):
    """Point MERLIN_HOME to a per-test temp dir so no test touches ~/.merlin/."""
    import paths

    monkeypatch.setenv("MERLIN_HOME", str(tmp_path))
    paths._dev_mode_override = None
    yield
    paths._dev_mode_override = None
