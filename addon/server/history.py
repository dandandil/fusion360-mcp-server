"""
History — journal of design edits for named checkpoints.

The API cannot read Fusion's undo list, so the journal mirrors it: MCP
transactions are recorded by the bridge, every other completed command
(UI edits, scripts) is seen via ``UserInterface.commandTerminated`` and
recorded as foreign.  Undo pops the newest entry; redo cannot be mirrored
reliably and is recorded as foreign too.  MCP edits made outside a
transaction (marker moves) are recorded as untracked: their undo behaviour
is unknown, so they block rollback like foreign entries.  Marker moves are
journaled as such and do not block: a checkpoint stores the marker position
and rollback restores it after undoing the MCP steps.

Rollback to a checkpoint undoes the MCP steps after it, and refuses when
foreign entries are in the way or the timeline count shows an edit the
journal did not see.
"""

import threading

import adsk.core
import adsk.fusion

from . import get_logger

log = get_logger("history")

UNDO_COMMAND_ID = "UndoCommand"
# Commands that complete without adding an undo step
IGNORED_COMMAND_IDS = frozenset({"SelectCommand"})

# MCP commands that change the design outside a transaction
UNTRACKED_EDIT_COMMANDS = frozenset({"set_timeline_marker", "advance_marker"})
MARKER_COMMANDS = frozenset({"set_timeline_marker", "advance_marker"})

MCP = "mcp"
FOREIGN = "foreign"
UNTRACKED = "untracked"
MARKER = "marker"
# Entry kinds rollback steps over without undoing them
NON_BLOCKING = (MCP, MARKER)


class HistoryError(Exception):
    def __init__(self, message: str, error_kind: str, hints: list[str]):
        super().__init__(message)
        self.error_kind = error_kind
        self.hints = hints


class _TerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def __init__(self, history: "History"):
        super().__init__()
        self._history = history

    def notify(self, args):
        self._history.on_command_terminated(args.commandId, args.terminationReason)


class History:
    def __init__(self, app, own_command_id: str, on_foreign=None):
        self._app = app
        self._own_command_id = own_command_id
        # Called on the main thread when the user changes the model
        self._on_foreign = on_foreign
        self._entries = []
        self._checkpoints = {}
        # Undo commands issued by rollback; their terminations must not pop
        self._expected_undos = 0
        # Foreign commands not yet reported to the client (socket thread reads)
        self._user_actions = []
        self._user_actions_lock = threading.Lock()
        self._terminated_handler = _TerminatedHandler(self)
        app.userInterface.commandTerminated.add(self._terminated_handler)

    def stop(self):
        try:
            self._app.userInterface.commandTerminated.remove(self._terminated_handler)
        except Exception as exc:
            log.warning("commandTerminated cleanup failed: %s", exc)

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    def record_step(self, tool: str):
        self._append(MCP, tool)

    def record_untracked(self, tool: str):
        self._append(MARKER if tool in MARKER_COMMANDS else UNTRACKED, tool)

    def on_command_terminated(self, command_id: str, reason):
        completed = adsk.core.CommandTerminationReason.CompletedTerminationReason
        if reason != completed:
            return
        if command_id == self._own_command_id or command_id in IGNORED_COMMAND_IDS:
            return
        if command_id == UNDO_COMMAND_ID:
            if self._expected_undos:
                self._expected_undos -= 1
            else:
                self._truncate(len(self._entries) - 1)
                self._note_user_action(command_id)
            return
        # debug: info would also print into execute_code output
        log.debug("Foreign command completed: %s", command_id)
        self._append(FOREIGN, command_id)
        self._note_user_action(command_id)

    def _note_user_action(self, command_id: str):
        with self._user_actions_lock:
            self._user_actions.append(command_id)
        if self._on_foreign is not None:
            self._on_foreign()

    def take_user_actions(self) -> list:
        with self._user_actions_lock:
            actions, self._user_actions = self._user_actions, []
        return actions

    def _append(self, kind: str, name: str):
        self._entries.append(
            {"kind": kind, "name": name, "timeline_count": self._timeline_count()}
        )

    def _truncate(self, length: int):
        del self._entries[max(length, 0) :]
        self._checkpoints = {
            name: cp
            for name, cp in self._checkpoints.items()
            if cp["position"] <= len(self._entries)
        }

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def create_checkpoint(self, name: str) -> dict:
        timeline = self._timeline()
        self._checkpoints[name] = {
            "position": len(self._entries),
            "document": self._document_name(),
            "timeline_count": self._timeline_count(),
            "marker_position": timeline.markerPosition if timeline else None,
        }
        return {"checkpoint": name, **self._checkpoints[name]}

    def list_checkpoints(self) -> dict:
        return {
            "checkpoints": [
                {
                    "name": name,
                    "document": cp["document"],
                    "steps_since": [
                        f"{e['kind']}:{e['name']}"
                        for e in self._entries[cp["position"] :]
                    ],
                }
                for name, cp in self._checkpoints.items()
            ],
            "journal": [f"{e['kind']}:{e['name']}" for e in self._entries],
        }

    def rollback_to_checkpoint(self, name: str, force: bool = False) -> dict:
        cp = self._checkpoints.get(name)
        if cp is None:
            raise HistoryError(
                f"Unknown checkpoint '{name}'",
                "INVALID_INPUT",
                ["call list_checkpoints to see the available names"],
            )
        if cp["document"] != self._document_name():
            raise HistoryError(
                f"Checkpoint '{name}' belongs to document '{cp['document']}'",
                "HISTORY_CONFLICT",
                ["activate that document first"],
            )
        steps = self._entries[cp["position"] :]
        blocking = [
            f"{e['kind']}:{e['name']}" for e in steps if e["kind"] not in NON_BLOCKING
        ]
        if blocking and not force:
            raise HistoryError(
                f"Steps not made by MCP transactions since '{name}': {blocking}",
                "HISTORY_CONFLICT",
                [
                    "undo them in the Fusion UI first",
                    "or pass force=true to undo them together with the MCP steps",
                ],
            )
        expected = steps[-1]["timeline_count"] if steps else cp["timeline_count"]
        current = self._timeline_count()
        if expected != current and not force:
            raise HistoryError(
                f"Timeline has {current} items, the journal expects {expected}: "
                "an edit happened that the journal did not see",
                "HISTORY_CONFLICT",
                ["inspect the timeline before rolling back"],
            )

        undo = self._app.userInterface.commandDefinitions.itemById(UNDO_COMMAND_ID)
        undone = [e for e in steps if e["kind"] != MARKER]
        for _ in undone:
            self._expected_undos += 1
            undo.execute()
            adsk.doEvents()
        self._truncate(cp["position"])
        marker = self._restore_marker(cp.get("marker_position"))

        after = self._timeline_count()
        return {
            "checkpoint": name,
            "undone": [f"{e['kind']}:{e['name']}" for e in undone],
            "timeline_count": after,
            "marker_position": marker,
            "timeline_matches_checkpoint": after == cp["timeline_count"],
        }

    def _restore_marker(self, position):
        timeline = self._timeline()
        if timeline is None or position is None:
            return None
        if timeline.markerPosition != position:
            if position >= timeline.count:
                timeline.moveToEnd()
            else:
                timeline.markerPosition = position
        return timeline.markerPosition

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _document_name(self):
        doc = self._app.activeDocument
        return doc.name if doc is not None else None

    def _timeline(self):
        """Timeline of a parametric design, else None."""
        doc = self._app.activeDocument
        if doc is None:
            return None
        try:
            design = doc.products.itemByProductType("DesignProductType")
        except Exception:  # raises for documents without a design product
            return None
        if design is None:
            return None
        if design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
            return None
        return design.timeline

    def _timeline_count(self):
        timeline = self._timeline()
        return timeline.count if timeline is not None else None
