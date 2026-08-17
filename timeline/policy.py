"""Product-level policy for records retained outside the Timeline view."""

from __future__ import annotations


# Older Timeline builds recorded every provider tool boundary. Those records
# remain valid private history, but they are too low-level for the development
# overview and must not consume query limits or span-index space.
HIDDEN_EVENT_KINDS = frozenset({"tool.call"})
