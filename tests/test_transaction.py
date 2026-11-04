"""EventBridge + TransactionRunner against a fake ``adsk`` runtime.

The fake reproduces the behaviour the add-in relies on:
``CommandDefinition.execute()`` only schedules the command, which runs
(created → execute → destroy) after the current event handler returns.
Whether Fusion really rolls back on ``executeFailed`` is checked live.
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


class _Event:
    def __init__(self):
        self.handlers = []

    def add(self, handler):
        self.handlers.append(handler)

    def remove(self, handler):
        self.handlers.remove(handler)


class _Args:
    def __init__(self, command=None):
        self.command = command
        self.executeFailed = False


class _Command:
    def __init__(self):
        self.execute = _Event()
        self.destroy = _Event()


class _CommandDefinition:
    def __init__(self, ui, cmd_id):
        self._ui = ui
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
            for h in list(self.commandCreated.handlers):
                h.notify(_Args(command))
            args = _Args(command)
            for h in list(command.execute.handlers):
                h.notify(args)
            self.undo_names.append(self.name)
            self.failed.append(args.executeFailed)
            for h in list(command.destroy.handlers):
                h.notify(_Args(command))

    def deleteMe(self):
        self._ui.commandDefinitions.defs.pop(self.id, None)


class _CommandDefinitions:
    def __init__(self, ui):
        self._ui = ui
        self.defs = {}

    def itemById(self, cmd_id):
        return self.defs.get(cmd_id)

    def addButtonDefinition(self, cmd_id, name, tooltip):
        self.defs[cmd_id] = _CommandDefinition(self._ui, cmd_id)
        return self.defs[cmd_id]


class _UI:
    def __init__(self):
        self.commandDefinitions = _CommandDefinitions(self)


class _App:
    def __init__(self):
        self.userInterface = _UI()

    def registerCustomEvent(self, event_id):
        return _Event()

    def unregisterCustomEvent(self, event_id):
        pass

    def fireCustomEvent(self, event_id):
        pass


class _Handler:
    """Stand-in for CommandHandler: records calls, fails on request."""

    def __init__(self):
        self.calls = []

    def execute_command(self, command):
        self.calls.append(command["type"])
        if command.get("params", {}).get("fail"):
            return {"status": "success", "result": {"ok": False, "error_kind": "X"}}
        return {"status": "success", "result": {"ok": True}}


def _fake_adsk():
    core = types.ModuleType("adsk.core")
    for name in (
        "CustomEventHandler",
        "CommandCreatedEventHandler",
        "CommandEventHandler",
    ):
        setattr(core, name, type(name, (), {"__init__": lambda self: None}))
    core.Application = _App
    adsk = types.ModuleType("adsk")
    adsk.core = core
    return adsk, core


@pytest.fixture(scope="module")
def addon(tmp_path_factory):
    adsk, core = _fake_adsk()
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "adsk", adsk)
        mp.setitem(sys.modules, "adsk.core", core)
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
    handler = _Handler()
    bridge = addon.bridge.EventBridge(app, handler)
    cmd_def = app.userInterface.commandDefinitions.itemById(addon.tx.COMMAND_ID)
    yield types.SimpleNamespace(bridge=bridge, handler=handler, cmd_def=cmd_def)
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


def test_timed_out_item_does_not_execute_late(setup):
    t, box = _submit(setup.bridge, "extrude", timeout=0.01)
    t.join(2)
    assert box["r"]["error_kind"] == "TIMEOUT"
    setup.bridge.drain_queue()  # skipped: cancelled before start
    setup.cmd_def.run_scheduled()
    assert setup.handler.calls == []


def test_stop_releases_in_flight_item(setup):
    t, box = _submit(setup.bridge, "extrude")
    _wait_queued(setup.bridge, 1)
    setup.bridge.drain_queue()
    setup.bridge.stop()
    t.join(2)
    assert box["r"]["error_kind"] == "TRANSACTION_FAILED"


def test_transactions_can_be_disabled(addon):
    app = _App()
    handler = _Handler()
    bridge = addon.bridge.EventBridge(app, handler, use_transactions=False)
    t, box = _submit(bridge, "extrude")
    _wait_queued(bridge, 1)
    bridge.drain_queue()
    t.join(2)
    bridge.stop()
    assert box["r"]["result"]["ok"] is True
    assert app.userInterface.commandDefinitions.defs == {}
