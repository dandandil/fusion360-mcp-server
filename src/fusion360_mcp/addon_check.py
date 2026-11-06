"""Warn when the add-in loaded in Fusion is not the repo's ``addon/``.

The server runs from the repo, the add-in is a copy on the Fusion machine;
a stale copy silently lacks parameters and tools.  Both sides hash the
add-in sources with ``addon/server/version.py``.
"""

import importlib.util
import logging
from pathlib import Path

log = logging.getLogger("fusion360_mcp.addon_check")

ADDON_DIR = Path(__file__).resolve().parents[2] / "addon"


def repo_addon_hash() -> str | None:
    """Hash of the repo's add-in, or None when not run from a checkout."""
    module_path = ADDON_DIR / "server" / "version.py"
    if not module_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_addon_version", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.addon_hash(ADDON_DIR)


def mismatch_warning(expected: str | None, ping_result: dict) -> str | None:
    """Warning text when the running add-in differs from *expected*."""
    if expected is None:
        return None
    actual = ping_result.get("addon_hash") if isinstance(ping_result, dict) else None
    if actual == expected:
        return None
    found = f"add-in {actual}" if actual else "an add-in too old to report its version"
    return (
        f"WARNING: Fusion runs {found}, the server expects add-in {expected}. "
        "Tools may lack parameters or fail. Copy the repo's addon/ to Fusion's "
        "AddIns folder and restart the add-in (Utilities > Add-Ins > Stop/Run)."
    )


class AddonCheck:
    """Pings once per connection and remembers the warning."""

    def __init__(self):
        self._expected = repo_addon_hash()
        self._checked = False
        self.warning = None

    def reset(self):
        self._checked = False

    def check(self, ping):
        """*ping()* returns the add-in's ping result."""
        if self._checked or self._expected is None:
            return self.warning
        try:
            self.warning = mismatch_warning(self._expected, ping())
        except Exception as exc:  # no connection yet: retry next call
            log.debug("add-in version check failed: %s", exc)
            return None
        self._checked = True
        if self.warning:
            log.warning(self.warning)
        return self.warning
