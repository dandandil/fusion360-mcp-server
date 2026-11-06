"""The server warns when Fusion runs a different add-in than the repo's."""

from fusion360_mcp.addon_check import AddonCheck, mismatch_warning, repo_addon_hash


def test_repo_hash_is_stable_and_short():
    assert repo_addon_hash() == repo_addon_hash()
    assert len(repo_addon_hash()) == 12


def test_matching_addon_has_no_warning():
    expected = repo_addon_hash()
    assert mismatch_warning(expected, {"addon_hash": expected}) is None


def test_old_or_other_addon_warns():
    assert "too old" in mismatch_warning("abc", {"ok": True, "status": "pong"})
    assert "add-in 123" in mismatch_warning("abc", {"addon_hash": "123"})


def test_check_pings_once_and_retries_after_failure():
    check = AddonCheck()
    calls = []

    def failing():
        calls.append(1)
        raise ConnectionError("down")

    assert check.check(failing) is None
    assert check.check(lambda: {"addon_hash": "old"}) is not None
    assert check.check(failing) is not None  # cached, no new ping
    assert calls == [1]
