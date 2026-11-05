"""
EventBridge — dispatches work from socket threads to Fusion's main thread.

Uses adsk.core.CustomEvent + a thread-safe queue so all Fusion API calls
happen on the main thread.  A 200 ms backup timer fires the custom event
periodically in case fireCustomEvent from a daemon thread is unreliable.

With transactions enabled, design-editing commands run one at a time inside
a Fusion Command (see transaction.py): the queue pauses while one is in
flight because the command executes after the event handler returns.

Jobs: ``start_job`` queues a command and returns at once, ``get_job`` reads
its state from the socket thread — for recomputes longer than the timeout.
``dry_run`` runs a command in a transaction that is always rolled back.
"""

import queue
import threading
import time
import traceback
import uuid

import adsk.core

from . import get_logger
from .hints import classify
from .history import UNTRACKED_EDIT_COMMANDS, History, HistoryError
from .transaction import COMMAND_ID, TransactionRunner, is_transactional

log = get_logger("bridge")

CUSTOM_EVENT_ID = "Fusion360MCP_BridgeEvent"
TIMER_INTERVAL_MS = 200  # backup polling interval
MAX_JOBS = 50  # finished jobs kept for get_job

# Commands the bridge runs itself: their state must survive reload_handler
BRIDGE_COMMANDS = frozenset(
    {
        "reload_handler",
        "create_checkpoint",
        "list_checkpoints",
        "rollback_to_checkpoint",
    }
)
# Answered on the socket thread, without waiting for the main thread
SOCKET_COMMANDS = frozenset({"ping", "start_job", "get_job", "dry_run"})


class WorkItem:
    """One unit of work submitted from a socket thread."""

    __slots__ = ("command", "result", "error", "done", "cancelled")

    def __init__(self, command: dict):
        self.command = command
        self.result = None
        self.error = None
        self.done = threading.Event()
        # Set when the waiting socket thread gave up (timeout).  A
        # cancelled item is skipped by drain_queue so a timed-out command
        # never executes late — the client has already reported failure.
        self.cancelled = False


class _MainThreadHandler(adsk.core.CustomEventHandler):
    """Attached to the CustomEvent; runs on Fusion's main thread."""

    def __init__(self, bridge: "EventBridge"):
        super().__init__()
        self._bridge = bridge

    def notify(self, args):  # called on main thread
        self._bridge.drain_queue()


class EventBridge:
    """
    Bridge between daemon socket threads and Fusion's main thread.

    Socket thread calls ``submit(command)`` which blocks (up to *timeout*
    seconds) until the main thread has executed the command and stored the
    result.
    """

    def __init__(
        self,
        app: adsk.core.Application,
        command_handler,
        use_transactions: bool = True,
    ):
        self._app = app
        self._handler = command_handler
        self._queue: queue.Queue = queue.Queue()
        self._jobs: dict = {}
        self._jobs_lock = threading.Lock()
        self._tx = None
        self._history = None
        if use_transactions:
            self._history = History(app, COMMAND_ID)
            self._tx = TransactionRunner(
                app.userInterface,
                self._execute,
                self._probe,
                self._history.record_step,
                self._poke,
            )

        # Register a custom event on the main thread
        self._event = app.registerCustomEvent(CUSTOM_EVENT_ID)
        self._event_handler = _MainThreadHandler(self)
        self._event.add(self._event_handler)

        # Backup timer thread — fires the custom event every 200 ms
        self._timer_running = True
        self._timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
        self._timer_thread.start()

        log.info(
            "EventBridge initialised (custom event: %s, transactions: %s)",
            CUSTOM_EVENT_ID,
            use_transactions,
        )

    # ------------------------------------------------------------------
    # Called from socket (daemon) threads
    # ------------------------------------------------------------------

    def submit(self, command: dict, timeout: float = 30.0) -> dict:
        """Queue *command* for main-thread execution; block until done."""
        cmd_type = command.get("type", "?")

        # Fast-path: ping never touches Fusion API — answer immediately
        if cmd_type == "ping":
            log.debug("ping (fast path)")
            return {"status": "success", "result": {"ok": True, "status": "pong"}}

        params = command.get("params", {})
        if cmd_type == "start_job":
            return self._start_job(params)
        if cmd_type == "get_job":
            return self._get_job(params.get("job_id"))
        try:
            command = self._resolve_dry_run(command)
        except ValueError as exc:
            return _failure("INVALID_INPUT", str(exc))

        log.debug("submit cmd=%s", cmd_type)
        item = self._enqueue(command)

        if not item.done.wait(timeout=timeout):
            # The client has given up.  Cancel the item so drain_queue
            # skips it instead of executing it late — a late execution
            # plus a client retry would apply mutations twice.
            item.cancelled = True
            log.warning("Command %s timed out after %ss (cancelled)", cmd_type, timeout)
            return {
                "status": "error",
                "error_kind": "TIMEOUT",
                "message": f"Command timed out after {timeout}s",
                "hints": ["run long commands with start_job and poll get_job"],
            }
        return self._response(item)

    def _enqueue(self, command: dict) -> WorkItem:
        item = WorkItem(command)
        self._queue.put(item)
        self._poke()
        return item

    def _response(self, item: WorkItem) -> dict:
        cmd_type = item.command.get("type", "?")
        if item.error is not None:
            log.error("Command %s failed: %s", cmd_type, item.error)
            kind, hints = classify(item.error)
            return {
                "status": "error",
                "error_kind": kind,
                "hints": hints,
                "message": item.error,
            }
        log.debug("Command %s completed", cmd_type)
        return item.result

    def _resolve_dry_run(self, command: dict) -> dict:
        """Turn ``dry_run(tool, params)`` into the tool's command, flagged."""
        if command.get("type") != "dry_run":
            return command
        params = command.get("params", {})
        tool = params.get("tool")
        if self._tx is None:
            raise ValueError("dry_run needs transactions (FUSION_MCP_TRANSACTIONS)")
        if tool in BRIDGE_COMMANDS or tool in SOCKET_COMMANDS:
            raise ValueError(f"{tool} cannot be dry-run")
        if not is_transactional(tool):
            raise ValueError(f"{tool} does not edit the design")
        return {"type": tool, "params": params.get("params", {}), "dry_run": True}

    # ------------------------------------------------------------------
    # Jobs (socket thread)
    # ------------------------------------------------------------------

    def _start_job(self, params: dict) -> dict:
        command = {"type": params.get("tool"), "params": params.get("params", {})}
        if command["type"] in ("start_job", "get_job", "ping"):
            return _failure("INVALID_INPUT", f"{command['type']} cannot be a job")
        try:
            command = self._resolve_dry_run(command)
        except ValueError as exc:
            return _failure("INVALID_INPUT", str(exc))
        job_id = uuid.uuid4().hex[:8]
        with self._jobs_lock:
            self._jobs[job_id] = {
                "item": self._enqueue(command),
                "tool": command["type"],
                "started": time.monotonic(),
            }
            self._prune_jobs()
        return {
            "status": "success",
            "result": {"ok": True, "job_id": job_id, "tool": command["type"]},
        }

    def _get_job(self, job_id) -> dict:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return _failure("INVALID_INPUT", f"Unknown job '{job_id}'")
        item = job["item"]
        info = {
            "job_id": job_id,
            "tool": job["tool"],
            "elapsed_s": round(time.monotonic() - job["started"], 1),
        }
        if not item.done.is_set():
            return {
                "status": "success",
                "result": {"ok": True, "state": "running", **info},
            }
        response = self._response(item)
        if response.get("status") == "error":
            result = {
                "ok": False,
                "error_kind": response.get("error_kind", "UNKNOWN"),
                "error_message": response.get("message", ""),
                "hints": response.get("hints", []),
            }
        else:
            result = dict(response["result"])
        return {"status": "success", "result": {**result, "state": "done", **info}}

    def _prune_jobs(self):
        done = [k for k, j in self._jobs.items() if j["item"].done.is_set()]
        for job_id in done[: max(len(done) - MAX_JOBS, 0)]:
            del self._jobs[job_id]

    # ------------------------------------------------------------------
    # Called on Fusion's main thread (from CustomEventHandler.notify)
    # ------------------------------------------------------------------

    def drain_queue(self):
        """Execute every queued work item (main thread only)."""
        while True:
            if self._tx is not None:
                self._tx.check_stalled()
                if self._tx.busy:
                    break
            try:
                item: WorkItem = self._queue.get_nowait()
            except queue.Empty:
                break

            cmd_type = item.command.get("type", "?")
            if item.cancelled:
                log.info("Skipping cancelled command %s", cmd_type)
                continue
            if (
                self._tx is not None
                and cmd_type not in BRIDGE_COMMANDS
                and is_transactional(cmd_type)
            ):
                self._tx.start(item)
                continue
            try:
                if cmd_type in BRIDGE_COMMANDS:
                    item.result = self._run_bridge_command(
                        cmd_type, item.command.get("params", {})
                    )
                else:
                    item.result = self._execute(item.command)
                    self._record_untracked(cmd_type, item.result)
            except Exception as exc:
                item.error = f"{exc}\n{traceback.format_exc()}"
                log.error("Main-thread exec of %s raised: %s", cmd_type, exc)
            finally:
                item.done.set()

    def _execute(self, command: dict) -> dict:
        return self._handler.execute_command(command)

    def _probe(self) -> dict:
        return self._handler.timeline_problems()

    def _record_untracked(self, cmd_type: str, response: dict):
        """Journal edits made outside a transaction, so rollback stops there."""
        result = response.get("result") if isinstance(response, dict) else None
        ok = isinstance(result, dict) and result.get("ok") is not False
        if self._history is not None and cmd_type in UNTRACKED_EDIT_COMMANDS and ok:
            self._history.record_untracked(cmd_type)

    def _run_bridge_command(self, cmd_type: str, params: dict) -> dict:
        if cmd_type == "reload_handler":
            # Main thread only: CommandHandler's constructor touches the API
            self.reload_handler()
            result = {"reloaded": True}
        else:
            try:
                if self._history is None:
                    raise HistoryError(
                        "Checkpoints need transactions",
                        "INVALID_INPUT",
                        ["unset FUSION_MCP_TRANSACTIONS=0 and restart the add-in"],
                    )
                result = getattr(self._history, cmd_type)(**params)
            except HistoryError as exc:
                result = {
                    "ok": False,
                    "error_kind": exc.error_kind,
                    "error_message": str(exc),
                    "hints": exc.hints,
                }
        result.setdefault("ok", True)
        return {"status": "success", "result": result}

    def _poke(self):
        try:
            self._app.fireCustomEvent(CUSTOM_EVENT_ID)
        except Exception:
            pass  # timer will pick it up

    # ------------------------------------------------------------------
    # Backup timer
    # ------------------------------------------------------------------

    def _timer_loop(self):
        interval = TIMER_INTERVAL_MS / 1000.0
        while self._timer_running:
            time.sleep(interval)
            # A busy transaction is polled too, so a command that never
            # starts is released by check_stalled instead of hanging.
            if not self._queue.empty() or (self._tx is not None and self._tx.busy):
                self._poke()

    # ------------------------------------------------------------------
    # Hot reload
    # ------------------------------------------------------------------

    def reload_handler(self):
        """Reimport handler modules and replace the active handler instance.

        Called on the main thread (from drain_queue).  Sibling modules
        are reloaded first, in dependency order, so command_handler picks
        up their changes too.
        """
        import importlib

        from . import command_handler as ch_mod
        from . import hints as hints_mod
        from . import hole_geometry as hole_mod
        from . import model_graph as graph_mod
        from . import parameter_units as units_mod

        for mod in (hints_mod, hole_mod, units_mod, graph_mod, ch_mod):
            importlib.reload(mod)
        self._handler = ch_mod.CommandHandler()
        # Reset lazy dispatch table so it picks up new commands
        self._handler.__class__._COMMANDS = None
        log.info("CommandHandler reloaded")

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def stop(self):
        self._timer_running = False
        # Release any queued items so blocked socket threads wake up
        # immediately instead of waiting out their full timeout against
        # a dead bridge.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            item.cancelled = True
            item.error = "Add-in stopped before the command executed"
            item.done.set()
        if self._tx is not None:
            self._tx.stop()
            self._history.stop()
        try:
            self._event.remove(self._event_handler)
        except Exception:
            pass
        try:
            self._app.unregisterCustomEvent(CUSTOM_EVENT_ID)
        except Exception:
            pass
        log.info("EventBridge stopped")


def _failure(error_kind: str, message: str) -> dict:
    """Application-level failure envelope (``ok: false``)."""
    return {
        "status": "success",
        "result": {"ok": False, "error_kind": error_kind, "error_message": message},
    }
