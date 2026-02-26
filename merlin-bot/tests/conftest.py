import sys
from pathlib import Path

# Add merlin-bot/ to sys.path so tests can import claude_wrapper, merlin_app, etc.
sys.path.insert(0, str(Path(__file__).parent.parent))

# Add project root so merlin_app can import tunnel, auth, etc.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
