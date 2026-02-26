import sys
from pathlib import Path

# Add project root to sys.path so tests can import core modules
sys.path.insert(0, str(Path(__file__).parent.parent))

# Also add merlin-bot/ for tests that reference bot modules (transcribe, etc.)
sys.path.insert(0, str(Path(__file__).parent.parent / "merlin-bot"))
