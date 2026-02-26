# /// script
# dependencies = ["python-dotenv"]
# ///
"""Print the dashboard URL with basic auth credentials embedded."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

user = os.getenv("DASHBOARD_USER", "admin")
password = os.getenv("DASHBOARD_PASS", "")

print(f"http://{user}:{password}@claude.nonocloud.fr:3123/overview")
