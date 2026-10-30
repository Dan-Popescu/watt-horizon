"""Controller safeguards; actual Docker isolation is verified separately on the managed daemon."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

SPEC = importlib.util.spec_from_file_location("cloud_pilot", Path(__file__).parents[1] / "tools/cloud_pilot.py")
assert SPEC is not None and SPEC.loader is not None
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def test_disk_ceiling_prevents_starting_a_batch(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "blob").write_bytes(b"original")
    args = SimpleNamespace(command=["radar", "tasks", "run"], mode="offline", max_bytes=1, reserve_bytes=0)
    with patch.object(pilot.subprocess, "Popen", side_effect=AssertionError("must not start Docker")):
        report = pilot.run_batch(args, tmp_path, {})
    assert report["state"] == "incomplete" and report["reason"] == "storage_budget"
    assert json.loads(Path(report["report"]).read_text())["state"] == "incomplete"


def test_free_space_reserve_and_no_symlink_traversal(tmp_path: Path) -> None:
    (tmp_path / "original").write_bytes(b"1234")
    (tmp_path / "link").symlink_to(tmp_path / "original")
    assert pilot.tree_bytes(tmp_path) == 4
    with patch.object(pilot.shutil, "disk_usage", return_value=SimpleNamespace(free=10)):
        assert not pilot.capacity(tmp_path, 100, 11)["ok"]


def test_fast_batch_exceeding_storage_is_still_incomplete(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    args = SimpleNamespace(command=["radar", "tasks", "run"], mode="offline", max_bytes=100, reserve_bytes=0)
    child = Mock()
    child.poll.return_value = 0
    child.wait.return_value = 0
    with (
        patch.object(pilot.subprocess, "Popen", return_value=child),
        patch.object(pilot, "capacity", side_effect=[{"ok": True}, {"ok": False}]),
    ):
        report = pilot.run_batch(args, tmp_path, {"WATT_PILOT_PROJECT": "test"})
    assert report["state"] == "incomplete" and report["reason"] == "storage_budget"


def test_initialization_preserves_existing_private_data(tmp_path: Path) -> None:
    root = tmp_path / "private"
    with patch.object(pilot.sys, "argv", ["cloud_pilot.py", "--root", str(root), "init"]):
        assert pilot.main() == 0
        token = (root / "control/review-token").read_bytes()
        (root / "inputs/network-smoke.json").write_text("operator-supplied")
        (root / "data/original").write_bytes(b"preserve")
        assert pilot.main() == 0
    assert (root / "control/review-token").read_bytes() == token
    assert (root / "inputs/network-smoke.json").read_text() == "operator-supplied"
    assert (root / "data/original").read_bytes() == b"preserve"
    assert not (root / "data/gold/labels.jsonl").exists()


def test_network_profile_fails_closed() -> None:
    policy = {"version": 1, "http_network_policy": {"type": "restricted", "egress_rules": [{"host": "github.com"}]}}
    assert pilot.missing_hosts(policy, ["www.nord.gouv.fr"]) == ["www.nord.gouv.fr"]
    with pytest.raises(ValueError, match="restricted"):
        pilot.missing_hosts({"version": 1, "http_network_policy": {"type": "unrestricted"}}, [])


def test_collect_cannot_launch_before_platform_policy_allows_sources(tmp_path: Path) -> None:
    args = SimpleNamespace(command=["radar", "tasks", "run"], mode="collect", max_bytes=100, reserve_bytes=0)
    policy = json.dumps({"version": 1, "http_network_policy": {"type": "restricted", "egress_rules": []}})
    original_read = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/etc/codex/network-policy.json":
            return policy
        return original_read(path)

    with (
        patch.object(Path, "read_text", read),
        patch.object(pilot.subprocess, "Popen", side_effect=AssertionError),
        pytest.raises(ValueError, match="Platform destinations missing"),
    ):
        pilot.run_batch(args, tmp_path, {})
