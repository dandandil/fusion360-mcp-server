"""EventBridge, TransactionRunner and History against a fake ``adsk`` runtime.

The fake reproduces the behaviour the add-in relies on:
``CommandDefinition.execute()`` only schedules the command, which runs
(created → execute → destroy) after the current event handler returns;
``executeFailed`` rolls the command's edits back; every completed command
fires ``commandTerminated``.  Whether Fusion really behaves like this is
checked live (see TIMELINE.md).
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_SERVER = REPO_ROOT / "addon" / "server"
COMPLETED = 1
PARAMETRIC = 1
UNDO = "UndoCommand"


class _Event:
    def __init__(self):
        self.handlers = []

    def add(self, handler):
        self.handlers.append(handler)

    def remove(self, handler):
        self.handlers.remove(handler)

    def fire(self, args):
        for h in list(self.handlers):
            h.notify(args)


class _Args:
    def __init__(self, command=None, command_id=None, reason=COMPLETED):
        self.command = command
        self.executeFailed = False
        self.commandId = command_id
        self.terminationReason = reason


class _Command:
    def __init__(self):
        self.execute = _Event()
        self.destroy = _Event()


class _CommandDefinition:
    def __init__(self, app, cmd_id):
        self._app = app
        self.id = cmd_id
        self.name = ""
        self.commandCreated = _Event()
        self.scheduled = 0
        self.undo_names = []
        self.failed = []
        self.start_command = True

    def execute(self):
        self.scheduled += 1
        return True

    def run_scheduled(self):
        """What Fusion does after the event handler that called execute()."""
        while self.scheduled:
            self.scheduled -= 1
            if not self.start_command:
                continue
            command = _Command()
            timeline_before = self._app.timeline.count
            problems_before = dict(self._app.problems)
            self.commandCreated.fire(_Args(command))
            args = _Args(command)
            command.execute.fire(args)
            if args.executeFailed:
                self._app.timeline.count = timeline_before
                self._app.problems = problems_before
            else:
                self.undo_names.append(self.name)
            self.failed.append(args.executeFailed)
            command.destroy.fire(_Args(command))
            self._app.terminate(self.id)

    def deleteMe(self):
        self._app.userInterface.commandDefinitions.defs.pop(self.id, None)


class _UndoDefinition:
    """Undo runs synchronously here; each step removed one timeline item."""

    def __init__(self, app):
        self._app = app

    def execute(self):
        self._app.timeline.count -= 1
        self._app.terminate(UNDO)
        return True


class _CommandDefinitions:
    def __init__(self, app):
        self._app = app
        self.defs = {UNDO: _UndoDefinition(app)}

    def itemById(self, cmd_id):
        return self.defs.get(cmd_id)

    def addButtonDefinition(self, cmd_id, name, tooltip):
        self.defs[cmd_id] = _CommandDefinition(self._app, cmd_id)
        return self.defs[cmd_id]


class _UI:
    def __init__(self, app):
        self.commandDefinitions = _CommandDefinitions(app)
        self.commandTerminated = _Event()


class _Design:
    def __init__(self, timeline):
        self.designType = PARAMETRIC
        self.timeline = timeline


class _Products:
    def __init__(self, design):
        self._design = design

    def itemByProductType(self, product_type):
        return self._design


class _App:
    def __init__(self):
        self.userInterface = _UI(self)
        self.timeline = types.SimpleNamespace(count=0)
        self.problems = {}
        self.names = []
        self.activeDocument = types.SimpleNamespace(
            name="Doc1", products=_Products(_Design(self.timeline))
        )

    def registerCustomEvent(self, event_id):
        return _Event()

    def unregisterCustomEvent(self, event_id):
        pass

    def fireCustomEvent(self, event_id):
        pass

    def terminate(self, command_id, reason=COMPLETED):
        self.userInterface.commandTerminated.fire(_Args(None, command_id, reason))

    def ui_edit(self, command_id="ExtrudeCommand"):
        """The user edits the design in Fusion's UI."""
        self.timeline.count += 1
        self.terminate(command_id)


class _Handler:
    """Stand-in for CommandHandler: records calls, edits, fails on request."""

    def __init__(self, app):
        self._app = app
        self.calls = []

    def timeline_state(self):
        return {"names": list(self._app.names), "problems": dict(self._app.problems)}

    def execute_command(self, command):
        self.calls.append(command["type"])
        params = command.get("params", {})
        if params.get("removes"):
            self._app.names.remove(params["removes"])
        if params.get("breaks"):
            self._app.problems[params["breaks"]] = {"health": "error"}
        if params.get("fail"):
            self._app.timeline.count += 1  # partial edit before the failure
            return {"status": "success", "result": {"ok": False, "error_kind": "X"}}
        if command["type"] not in ("get_scene_info", "set_timeline_marker"):
            self._app.timeline.count += 1
        return {"status": "success", "result": {"ok": True}}


def _fake_adsk():
    core = types.ModuleType("adsk.core")
    for name in (
        "CustomEventHandler",
        "CommandCreatedEventHandler",
        "CommandEventHandler",
        "ApplicationCommandEventHandler",
    ):
        setattr(core, name, type(name, (), {"__init__": lambda self: None}))
    core.Application = _App
    core.CommandTerminationReason = types.SimpleNamespace(
        CompletedTerminationReason=COMPLETED
    )
    fusion = types.ModuleType("adsk.fusion")
    fusion.DesignTypes = types.SimpleNamespace(ParametricDesignType=PARAMETRIC)
    adsk = types.ModuleType("adsk")
    adsk.core = core
    adsk.fusion = fusion
    adsk.doEvents = lambda: None
    return adsk, core, fusion


@pytest.fixture(scope="module")
def addon(tmp_path_factory):
    adsk, core, fusion = _fake_adsk()
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "adsk", adsk)
        mp.setitem(sys.modules, "adsk.core", core)
        mp.setitem(sys.modules, "adsk.fusion", fusion)
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))  # add-in log file
        spec = importlib.util.spec_from_file_location(
            "_addon_server",
            ADDON_SERVER / "__init__.py",
            submodule_search_locations=[str(ADDON_SERVER)],
        )
        pkg = importlib.util.module_from_spec(spec)
        mp.setitem(sys.modules, "_addon_server", pkg)
        spec.loader.exec_module(pkg)
        bridge_mod = importlib.import_module("_addon_server.event_bridge")
        tx_mod = importlib.import_module("_addon_server.transaction")
        yield types.SimpleNamespace(bridge=bridge_mod, tx=tx_mod)
        for name in [m for m in sys.modules if m.startswith("_addon_server")]:
            del sys.modules[name]


@pytest.fixture
def setup(addon):
    app = _App()
    handler = _Handler(app)
    bridge = addon.bridge.EventBridge(app, handler)
    cmd_def = app.userInterface.commandDefinitions.itemById(addon.tx.COMMAND_ID)
    yield types.SimpleNamespace(
        app=app, bridge=bridge, handler=handler, cmd_def=cmd_def
    )
    bridge.stop()


def _submit(bridge, cmd_type, timeout=5.0, **params):
    """Submit from a socket-like thread; returns (thread, response box)."""
    box = {}
    t = threading.Thread(
        target=lambda: box.update(
            r=bridge.submit({"type": cmd_type, "params": params}, timeout=timeout)
        )
    )
    t.start()
    return t, box


def _wait_queued(bridge, n):
    for _ in range(500):
        if bridge._queue.qsize() >= n:
            return
        threading.Event().wait(0.01)
    raise AssertionError("commands were not queued")


def test_read_only_command_runs_outside_transaction(setup):
    t, box = _submit(setup.bridge, "get_scene_info")
    _wait_queued(setup.bridge, 1)
    setup.bridge.drain_queue()
    t.join(2)
    assert box["r"]["result"]["ok"] is True
    assert setup.cmd_def.scheduled == 0


def test_reload_handler_runs_outside_transaction(setup, monkeypatch):
    monkeypatch.setattr(setup.bridge, "reload_handler", lambda: None)
    t, box = _submit(setup.bridge, "reload_handler")
    _wait_queued(setup.bridge, 1)
    setup.bridge.drain_queue()
    t.join(2)
    assert box["r"]["result"]["reloaded"] is True
    assert setup.cmd_def.scheduled == 0


def test_mutation_runs_inside_named_command(setup):
    t, box = _submit(setup.bridge, "extrude")
    _wait_queued(setup.bridge, 1)
    setup.bridge.drain_queue()
    assert setup.handler.calls == []  # deferred until the command runs
    setup.cmd_def.run_scheduled()
    t.join(2)
    assert box["r"]["result"]["ok"] is True
    assert setup.cmd_def.undo_names == ["MCP: extrude"]
    assert setup.cmd_def.failed == [False]


def test_failure_aborts_command_and_reports_rollback(setup):
    t, box = _submit(setup.bridge, "execute_code", fail=True)
    _wait_queued(setup.bridge, 1)
    setup.bridge.drain_queue()
    setup.cmd_def.run_scheduled()
    t.join(2)
    assert setup.cmd_def.failed == [True]
    assert box["r"]["result"]["rolled_back"] is True
    assert setup.app.timeline.count == 0


def test_mutations_run_one_at_a_time(setup):
    t1, _ = _submit(setup.bridge, "extrude")
    _wait_queued(setup.bridge, 1)
    t2, _ = _submit(setup.bridge, "fillet")
    _wait_queued(setup.bridge, 2)
    setup.bridge.drain_queue()
    assert setup.cmd_def.scheduled == 1  # second waits for the first
    setup.cmd_def.run_scheduled()
    setup.bridge.drain_queue()
    setup.cmd_def.run_scheduled()
    t1.join(2)
    t2.join(2)
    assert setup.handler.calls == ["extrude", "fillet"]
    assert setup.cmd_def.undo_names == ["MCP: extrude", "MCP: fillet"]


def test_command_that_never_starts_is_released(setup, addon, monkeypatch):
    monkeypatch.setattr(addon.tx, "START_TIMEOUT_S", 0.0)
    setup.cmd_def.start_command = False
    t, box = _submit(setup.bridge, "extrude")
    _wait_queued(setup.bridge, 1)
    setup.bridge.drain_queue()
    setup.cmd_def.run_scheduled()
    setup.bridge.drain_queue()
    t.join(2)
    assert box["r"]["status"] == "error"
    assert box["r"]["error_kind"] == "TRANSACTION_FAILED"
    assert setup.handler.calls == []


def test_timed_out_item_becomes_a_job(setup):
    t, box = _submit(setup.bridge, "extrude", timeout=0.01)
    t.join(2)
    running = box["r"]["result"]
    assert running["state"] == "running"
    setup.bridge.drain_queue()  # still executes: the client polls it
    setup.cmd_def.run_scheduled()
    assert setup.handler.calls == ["extrude"]
    done = setup.bridge.submit(
        {"type": "get_job", "params": {"job_id": running["job_id"]}}
    )["result"]
    assert (done["state"], done["ok"]) == ("done", True)


def test_stop_releases_in_flight_item(setup):
    t, box = _submit(setup.bridge, "extrude")
    _wait_queued(setup.bridge, 1)
    setup.bridge.drain_queue()
    setup.bridge.stop()
    t.join(2)
    assert box["r"]["error_kind"] == "TRANSACTION_FAILED"


def test_transactions_can_be_disabled(addon):
    app = _App()
    handler = _Handler(app)
    bridge = addon.bridge.EventBridge(app, handler, use_transactions=False)
    t, box = _submit(bridge, "extrude")
    _wait_queued(bridge, 1)
    bridge.drain_queue()
    t.join(2)
    bridge.stop()
    assert box["r"]["result"]["ok"] is True
    assert addon.tx.COMMAND_ID not in app.userInterface.commandDefinitions.defs


# ── history ──────────────────────────────────────────────────────────


def _call(setup, cmd_type, **params):
    """Run one command end to end, as Fusion would; return its result."""
    t, box = _submit(setup.bridge, cmd_type, **params)
    _wait_queued(setup.bridge, 1)
    setup.bridge.drain_queue()
    setup.cmd_def.run_scheduled()
    t.join(2)
    return box["r"]["result"]


def test_rollback_undoes_mcp_steps_after_checkpoint(setup):
    _call(setup, "extrude")
    _call(setup, "create_checkpoint", name="cp")
    _call(setup, "fillet")
    _call(setup, "chamfer", fail=True)  # rolled back: not a step
    _call(setup, "shell")
    steps = _call(setup, "list_checkpoints")["checkpoints"][0]["steps_since"]
    assert steps == ["mcp:fillet", "mcp:shell"]

    result = _call(setup, "rollback_to_checkpoint", name="cp")
    assert result["ok"] is True
    assert result["undone"] == ["mcp:fillet", "mcp:shell"]
    assert result["timeline_matches_checkpoint"] is True
    assert setup.app.timeline.count == 1
    assert _call(setup, "list_checkpoints")["journal"] == ["mcp:extrude"]


def test_rollback_refuses_after_foreign_edit(setup):
    _call(setup, "create_checkpoint", name="cp")
    _call(setup, "extrude")
    setup.app.ui_edit("FilletCommand")
    result = _call(setup, "rollback_to_checkpoint", name="cp")
    assert result["ok"] is False
    assert result["error_kind"] == "HISTORY_CONFLICT"
    assert "FilletCommand" in result["error_message"]
    assert setup.app.timeline.count == 2

    forced = _call(setup, "rollback_to_checkpoint", name="cp", force=True)
    assert forced["undone"] == ["mcp:extrude", "foreign:FilletCommand"]
    assert setup.app.timeline.count == 0


def test_user_undo_pops_journal(setup):
    _call(setup, "create_checkpoint", name="cp")
    _call(setup, "extrude")
    _call(setup, "fillet")
    setup.app.userInterface.commandDefinitions.itemById(UNDO).execute()  # Ctrl+Z
    result = _call(setup, "rollback_to_checkpoint", name="cp")
    assert result["undone"] == ["mcp:extrude"]
    assert setup.app.timeline.count == 0


def test_rollback_refuses_on_unseen_timeline_change(setup):
    _call(setup, "create_checkpoint", name="cp")
    _call(setup, "extrude")
    setup.app.timeline.count += 1  # an edit that fired no commandTerminated
    result = _call(setup, "rollback_to_checkpoint", name="cp")
    assert result["error_kind"] == "HISTORY_CONFLICT"
    assert "journal expects 1" in result["error_message"]


def test_rollback_to_unknown_checkpoint(setup):
    result = _call(setup, "rollback_to_checkpoint", name="nope")
    assert result["error_kind"] == "INVALID_INPUT"


def test_undo_past_checkpoint_drops_it(setup):
    _call(setup, "extrude")
    _call(setup, "create_checkpoint", name="cp")
    setup.app.userInterface.commandDefinitions.itemById(UNDO).execute()
    assert _call(setup, "list_checkpoints")["checkpoints"] == []


# ── jobs, dry runs, untracked edits ──────────────────────────────────


def test_dry_run_reports_problems_and_rolls_back(setup):
    result = _call(setup, "dry_run", tool="set_parameter", params={"breaks": "Fillet1"})
    assert result["rolled_back"] is True
    assert result["dry_run"]["new_problems"] == {"Fillet1": {"health": "error"}}
    assert setup.cmd_def.failed == [True]
    assert setup.app.timeline.count == 0
    assert setup.app.problems == {}
    assert _call(setup, "list_checkpoints")["journal"] == []


def test_edit_reports_cascade_removed_items(setup):
    setup.app.names = ["Extrude18", "OffsetFaces5"]
    result = _call(setup, "execute_code", removes="OffsetFaces5")
    assert result["timeline_changes"] == {"removed_items": ["OffsetFaces5"]}
    assert "timeline_changes" not in _call(setup, "extrude")


def test_user_actions_are_reported_once(setup):
    setup.app.ui_edit("ExtrudeCommand")
    assert _call(setup, "get_scene_info")["user_actions"] == ["ExtrudeCommand"]
    assert "user_actions" not in _call(setup, "get_scene_info")


def test_dry_run_rejects_read_only_tool(setup):
    response = setup.bridge.submit(
        {"type": "dry_run", "params": {"tool": "get_scene_info"}}
    )
    assert response["result"]["error_kind"] == "INVALID_INPUT"


def test_job_returns_at_once_and_reports_result(setup):
    started = setup.bridge.submit(
        {"type": "start_job", "params": {"tool": "extrude", "params": {}}}
    )["result"]
    job = {"type": "get_job", "params": {"job_id": started["job_id"]}}
    assert setup.bridge.submit(job)["result"]["state"] == "running"
    setup.bridge.drain_queue()
    setup.cmd_def.run_scheduled()
    done = setup.bridge.submit(job)["result"]
    assert done["state"] == "done"
    assert done["ok"] is True
    assert done["tool"] == "extrude"


def test_job_can_wrap_a_dry_run(setup):
    started = setup.bridge.submit(
        {
            "type": "start_job",
            "params": {"tool": "dry_run", "params": {"tool": "extrude"}},
        }
    )["result"]
    setup.bridge.drain_queue()
    setup.cmd_def.run_scheduled()
    done = setup.bridge.submit(
        {"type": "get_job", "params": {"job_id": started["job_id"]}}
    )["result"]
    assert done["rolled_back"] is True
    assert setup.app.timeline.count == 0


def test_unknown_job(setup):
    response = setup.bridge.submit({"type": "get_job", "params": {"job_id": "x"}})
    assert response["result"]["error_kind"] == "INVALID_INPUT"


def test_marker_move_blocks_rollback(setup):
    _call(setup, "create_checkpoint", name="cp")
    _call(setup, "extrude")
    _call(setup, "set_timeline_marker", position=0)
    assert setup.cmd_def.undo_names == ["MCP: extrude"]  # marker: no transaction
    result = _call(setup, "rollback_to_checkpoint", name="cp")
    assert result["error_kind"] == "HISTORY_CONFLICT"
    assert "untracked:set_timeline_marker" in result["error_message"]
