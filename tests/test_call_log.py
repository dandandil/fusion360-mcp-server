"""Call log lines: arguments kept (long text cut), bulky results summarised."""

import json
import time

from fusion360_mcp.call_log import MAX_TEXT, CallLog


def _lines(directory):
    (path,) = directory.glob("calls-*.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_record_keeps_flags_and_summarises_result(tmp_path):
    calls = CallLog(tmp_path)
    result = {
        "ok": True,
        "timeline_changes": {"removed_items": ["OffsetFaces5"]},
        "image_base64": "x" * 10_000,
    }
    calls.record("delete_feature", {"name": "Extrude18"}, time.monotonic(), result)
    (entry,) = _lines(tmp_path)
    assert entry["tool"] == "delete_feature"
    assert entry["timeline_changes"] == {"removed_items": ["OffsetFaces5"]}
    assert entry["result_keys"] == ["image_base64"]
    assert "image_base64" not in entry
    assert (tmp_path / "server.log").exists()


def test_long_arguments_are_cut(tmp_path):
    calls = CallLog(tmp_path)
    code = "print(1)\n" * 1000
    calls.record("execute_code", {"code": code}, time.monotonic(), {"ok": True})
    (entry,) = _lines(tmp_path)
    assert len(entry["arguments"]["code"]) < MAX_TEXT + 40


def test_disabled_log_writes_nothing(tmp_path):
    CallLog(None).record("ping", {}, time.monotonic(), {"ok": True})
    assert list(tmp_path.iterdir()) == []
