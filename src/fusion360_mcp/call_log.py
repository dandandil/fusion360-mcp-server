"""Call log: one JSON line per tool call, for reviewing agent sessions.

Written next to the server (``FUSION_MCP_LOG_DIR``, default
``~/.local/state/fusion360-mcp``): ``calls-YYYY-MM-DD.jsonl`` and
``server.log``.  ``FUSION_MCP_LOG_DIR=off`` disables both.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path

log = logging.getLogger("fusion360_mcp.call_log")

MAX_TEXT = 4000  # execute_code sources are kept; longer strings are cut
MAX_MESSAGE = 500
# Result fields worth keeping; the rest (geometry, images) is summarised
KEPT_RESULT_KEYS = (
    "ok",
    "error_kind",
    "rolled_back",
    "state",
    "job_id",
    "timeline_changes",
    "user_actions",
    "dry_run",
    "recreated",
    "problems",
)


def log_dir() -> Path | None:
    raw = os.environ.get("FUSION_MCP_LOG_DIR", "~/.local/state/fusion360-mcp")
    if raw.lower() == "off":
        return None
    return Path(raw).expanduser()


def _cut(value):
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + f"... [{len(value)} chars]"
    if isinstance(value, dict):
        return {k: _cut(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v) for v in value]
    return value


class CallLog:
    def __init__(self, directory: Path | None):
        self._dir = directory
        self._session = uuid.uuid4().hex[:8]
        if directory is None:
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(directory / "server.log", encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            logging.getLogger("fusion360_mcp").addHandler(handler)
        except OSError as exc:
            log.warning("Call log disabled: %s", exc)
            self._dir = None

    def record(self, tool: str, arguments: dict, started: float, outcome: dict):
        """*outcome*: the add-in result, or {"ok": False, "error_kind", "message"}."""
        if self._dir is None:
            return
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session": self._session,
            "tool": tool,
            "arguments": _cut(arguments),
            "duration_s": round(time.monotonic() - started, 3),
            **summarise(outcome),
        }
        path = self._dir / f"calls-{time.strftime('%Y-%m-%d')}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            log.warning("Call log write failed: %s", exc)


def summarise(result) -> dict:
    if not isinstance(result, dict):
        return {"result": str(result)[:MAX_MESSAGE]}
    summary = {k: result[k] for k in KEPT_RESULT_KEYS if k in result}
    for key in ("error_message", "message"):
        if result.get(key):
            summary["message"] = str(result[key])[:MAX_MESSAGE]
    summary["result_keys"] = sorted(k for k in result if k not in KEPT_RESULT_KEYS)
    summary["result_chars"] = len(json.dumps(result, default=str))
    return summary
