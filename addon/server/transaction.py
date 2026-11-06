"""
Transactions — run one command inside a Fusion Command.

Edits made in a Command's execute handler form a single undo step named
after the command; setting ``executeFailed`` aborts that step and rolls
the edits back.  ``CommandDefinition.execute()`` is deferred until the
current event handler returns, so completion is reported via callback.

The timeline is probed before and after each edit: removed items (cascade
deletes) and new problems are reported as ``timeline_changes``.  A dry run
(command flagged ``dry_run``) is always rolled back and reports the same
diff as ``dry_run``.
"""

import time
import traceback

import adsk.core

from . import get_logger

log = get_logger("transaction")

COMMAND_ID = "Fusion360MCP_Transaction"
UNDO_NAME_PREFIX = "MCP: "
START_TIMEOUT_S = 5.0

# Commands that never edit the design, or that drive the UI themselves
# (undo, workspace switches, exports, CAM futures) and must not run nested
# inside another command.
NON_TRANSACTIONAL_COMMANDS = frozenset(
    {
        # read-only
        "ping",
        "get_scene_info",
        "get_object_info",
        "get_bounding_box",
        "list_components",
        "get_timeline",
        "get_feature_info",
        "get_dependents",
        "features_near",
        "get_design_notes",
        "snapshot_references",
        "check_references",
        "get_parameters",
        "get_physical_properties",
        "measure_distance",
        "measure_angle",
        "check_interference",
        "compare_meshes",
        "get_design_type",
        "render_view",
        "cam_list_setups",
        "cam_list_operations",
        "cam_get_operation_info",
        # drive the UI or wait on Fusion; marker moves are journaled as
        # untracked by the bridge (their undo behaviour is unverified)
        "set_timeline_marker",
        "compute_all",
        "undo",
        "set_design_type",
        "export",
        "export_stl",
        "export_step",
        "export_f3d",
        "export_view_sheet",
        "cam_create_setup",
        "cam_create_operation",
        "cam_generate_toolpath",
        "cam_post_process",
    }
)


def is_transactional(cmd_type: str) -> bool:
    return cmd_type not in NON_TRANSACTIONAL_COMMANDS


def problem_diff(before: dict, after: dict) -> dict:
    """Compare two {name: {"health", "message"}} maps of timeline problems."""
    new = {
        name: problem
        for name, problem in after.items()
        if before.get(name, {}).get("health") != problem.get("health")
    }
    resolved = sorted(name for name in before if name not in after)
    return {"new_problems": new, "resolved_problems": resolved}


def timeline_changes(before: dict, after: dict) -> dict:
    """Diff two {"names", "problems"} timeline probes."""
    remaining = set(after["names"])
    removed = [name for name in before["names"] if name not in remaining]
    return {
        "removed_items": removed,
        **problem_diff(before["problems"], after["problems"]),
    }


def _is_failure(response: dict) -> bool:
    if response.get("status") == "error":
        return True
    result = response.get("result")
    return isinstance(result, dict) and result.get("ok") is False


class _CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, runner: "TransactionRunner"):
        super().__init__()
        self._runner = runner

    def notify(self, args):
        self._runner._on_created(args.command)


class _ExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, runner: "TransactionRunner"):
        super().__init__()
        self._runner = runner

    def notify(self, args):
        self._runner._on_execute(args)


class _DestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self, runner: "TransactionRunner"):
        super().__init__()
        self._runner = runner

    def notify(self, args):
        self._runner._on_destroy()


class TransactionRunner:
    """Runs work items one at a time, each inside its own Fusion Command.

    *execute(command) -> response* does the work; *probe()* reads the
    timeline names and problems (None if unreadable); *on_committed(cmd_type)*
    is called for each successful (not rolled back) item; *on_finished()* is
    called on the main thread once an item is done so the caller can continue.
    """

    def __init__(self, ui, execute, probe, on_committed, on_finished):
        defs = ui.commandDefinitions
        self._cmd_def = defs.itemById(COMMAND_ID) or defs.addButtonDefinition(
            COMMAND_ID, "MCP", "Fusion360MCP transaction"
        )
        self._execute = execute
        self._probe = probe
        self._on_committed = on_committed
        self._on_finished = on_finished
        self._created_handler = _CreatedHandler(self)
        self._cmd_def.commandCreated.add(self._created_handler)
        # Fusion keeps only weak references to handlers
        self._command_handlers = []
        self._item = None
        self._started_at = 0.0
        self._created = False

    @property
    def busy(self) -> bool:
        return self._item is not None

    def start(self, item):
        cmd_type = item.command.get("type", "?")
        self._item = item
        self._started_at = time.monotonic()
        self._created = False
        self._cmd_def.name = UNDO_NAME_PREFIX + cmd_type
        log.debug("transaction start cmd=%s", cmd_type)
        if not self._cmd_def.execute():
            self._finish(self._error("CommandDefinition.execute() returned False"))

    def check_stalled(self):
        """Release an item whose command never started (main thread)."""
        if not self.busy or self._created:
            return
        if time.monotonic() - self._started_at > START_TIMEOUT_S:
            self._finish(
                self._error(
                    f"Transaction command did not start within "
                    f"{START_TIMEOUT_S}s; the design was not modified"
                )
            )

    def stop(self):
        if self.busy:
            self._finish(self._error("Add-in stopped before the command executed"))
        try:
            self._cmd_def.commandCreated.remove(self._created_handler)
            self._cmd_def.deleteMe()
        except Exception as exc:
            log.warning("Transaction command cleanup failed: %s", exc)

    # ------------------------------------------------------------------
    # Command events (main thread)
    # ------------------------------------------------------------------

    def _on_created(self, command):
        self._created = True
        execute_handler = _ExecuteHandler(self)
        destroy_handler = _DestroyHandler(self)
        command.execute.add(execute_handler)
        command.destroy.add(destroy_handler)
        self._command_handlers = [execute_handler, destroy_handler]

    def _on_execute(self, args):
        item = self._item
        if item is None or item.cancelled:
            return
        dry_run = item.command.get("dry_run", False)
        try:
            before = self._probe()
            response = self._execute(item.command)
            if not _is_failure(response):
                self._attach_changes(response, before, dry_run)
        except Exception:
            response = self._error(traceback.format_exc())
        if dry_run or _is_failure(response):
            args.executeFailed = True
            result = response.get("result")
            if isinstance(result, dict):
                result["rolled_back"] = True
        else:
            self._on_committed(item.command.get("type", "?"))
        item.result = response

    def _attach_changes(self, response: dict, before, dry_run: bool):
        after = self._probe()
        result = response.get("result")
        if before is None or after is None or not isinstance(result, dict):
            return
        changes = timeline_changes(before, after)
        if dry_run:
            result["dry_run"] = changes
            return
        changes = {key: value for key, value in changes.items() if value}
        if changes:
            result["timeline_changes"] = changes

    def _on_destroy(self):
        self._command_handlers = []
        item = self._item
        if item is None:
            return
        if item.result is None:
            item.result = self._error("Transaction command ended before execution")
        self._finish(item.result)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _finish(self, response: dict):
        item, self._item = self._item, None
        item.result = response
        item.done.set()
        self._on_finished()

    @staticmethod
    def _error(message: str) -> dict:
        return {
            "status": "error",
            "error_kind": "TRANSACTION_FAILED",
            "message": message,
        }
