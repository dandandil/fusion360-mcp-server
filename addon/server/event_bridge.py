"""
EventBridge — dispatches work from socket threads to Fusion's main thread.

Uses adsk.core.CustomEvent + a thread-safe queue so all Fusion API calls
happen on the main thread.  A 200 ms backup timer fires the custom event
periodically in case fireCustomEvent from a daemon thread is unreliable.

With transactions enabled, design-editing commands run one at a time inside
a Fusion Command (see transaction.py): the queue pauses while one is in
flight because the command executes after the event handler returns.
"""

import queue
import threading
import time
import traceback

import adsk.core

from . import get_logger
from .hints import classify
from .history import History, HistoryError
from .transaction import COMMAND_ID, TransactionRunner, is_transactional

log = get_logger("bridge")

CUSTOM_EVENT_ID = "Fusion360MCP_BridgeEvent"
TIMER_INTERVAL_MS = 200  # backup polling interval

# Commands the bridge runs itself: their state must survive reload_handler
BRIDGE_COMMANDS = frozenset(
    {
        "reload_handler",
        "create_checkpoint",
        "list_checkpoints",
        "rollback_to_checkpoint",
    }
)


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
        self._tx = None
        self._history = None
        if use_transactions:
            self._history = History(app, COMMAND_ID)
            self._tx = TransactionRunner(
                app.userInterface,
                self._execute,
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

        log.debug("submit cmd=%s", cmd_type)
        item = WorkItem(command)
        self._queue.put(item)

        self._poke()

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
            }

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
            except Exception as exc:
                item.error = f"{exc}\n{traceback.format_exc()}"
                log.error("Main-thread exec of %s raised: %s", cmd_type, exc)
            finally:
                item.done.set()

    def _execute(self, command: dict) -> dict:
        return self._handler.execute_command(command)

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
