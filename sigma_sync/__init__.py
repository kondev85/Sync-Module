"""Read-only SiGMA Match attendee importer, sharing the existing Notion client."""

from pathlib import Path
import sys

# The original tools use sibling imports (``import config``). Keep those tools
# untouched while making them available from both the menu and ``python -m``.
_shared = str(Path(__file__).resolve().parent.parent / "swapcard_sync")
if _shared not in sys.path:
    sys.path.insert(0, _shared)
