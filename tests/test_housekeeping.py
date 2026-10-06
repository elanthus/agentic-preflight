import json
import os
from pathlib import Path

from agentic_preflight import housekeeping, runs
from agentic_preflight.store import Store
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def _record(store: Store, run_id: str, *, state: str, worktree: Path, source: Path) -> None:
    path = store.run_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "state": state,
                "worktree_path": str(worktree),
                "source_worktree_path": str(source),
            }
        )
    )


def _layout(repo: Path, tmp_path: Path) -> tuple[Store, Path]:
    root = tmp_path / "checkouts"
    store = Store(tmp_path / "state", worktrees_root=root)
    git("worktree", "add", "-b", "ap/leased", str(root / "leased"), "main", cwd=repo)
    for name in ("active", "finished", "runner"):
        git("worktree", "add", "--detach", str(root / name), "main", cwd=repo)
    write(root, "loose/cache.bin", "x" * 1000)
    _record(store, "run-active", state="LINT_PENDING", worktree=root / "active", source=repo)
    _record(store, "run-done", state="DONE", worktree=root / "finished", source=tmp_path / "gone")
    _record(store, "run-reused", state="DONE", worktree=root / "runner", source=repo)
    return store, root


def _by_name(report: dict) -> dict[str, dict]:
    return {Path(item["path"]).name: item for item in report["checkouts"]}


def test_inventory_classifies_registered_leased_and_owned_checkouts(feature_repo, tmp_path):
    store, _ = _layout(feature_repo, tmp_path)

    report = housekeeping.inventory(store, feature_repo)
    items = _by_name(report)

    assert set(items) == {"leased", "active", "finished", "runner", "loose"}
    assert items["leased"]["registered"]
    assert items["leased"]["leased"]
    assert items["leased"]["branch"] == "ap/leased"
    assert items["leased"]["status"] == "retained"
    assert items["active"]["detached"]
    assert not items["active"]["leased"]
    assert items["active"]["owner_run_id"] == "run-active"
    assert items["active"]["status"] == "retained"
    assert items["finished"]["owner_run_id"] == "run-done"
    assert items["finished"]["status"] == "reclaimable"
    assert items["runner"]["owner_run_id"] == "run-reused"
    assert items["runner"]["status"] == "retained"
    assert items["runner"]["reason"] == "reusable cache"
    assert not items["loose"]["registered"]
    assert items["loose"]["bytes"] == 1000
    assert items["loose"]["status"] == "reclaimable"
    assert all(item["bytes"] > 0 for item in items.values())
    assert report["total_bytes"] == sum(item["bytes"] for item in items.values())
    assert report["reclaimable_bytes"] == items["finished"]["bytes"] + 1000
    assert report["ap_branches"] == 1
    assert report["next_command"] == "agentic-preflight gc"
    assert report["noisy"] is False


def test_inventory_counts_an_unreadable_record_as_an_owner(feature_repo, tmp_path):
    store, _ = _layout(feature_repo, tmp_path)
    broken = store.run_path("run-broken")
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json")

    items = _by_name(housekeeping.inventory(store, feature_repo))

    assert items["finished"]["status"] == "retained"
    assert "run-broken" in items["finished"]["reason"]
    assert items["loose"]["status"] == "retained"


def test_inventory_does_not_follow_symlinks(tmp_path):
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "data").write_bytes(b"x" * 5000)
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "small").write_bytes(b"x" * 10)
    os.symlink(tmp_path / "big", tmp_path / "dir" / "link")

    assert housekeeping.directory_size(tmp_path / "dir") < 5000


def test_strict_start_reports_its_checkout_retained_and_a_leftover_reclaimable(feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation")
    worktrees_dir = runs.open_session(feature_repo).store.worktrees_dir
    leftover = worktrees_dir / "old-run"
    git("worktree", "add", "--detach", str(leftover), "main", cwd=feature_repo)

    started = ScriptedAgent(feature_repo).run("start", "--intent", "prepare feature x")

    report = started["data"]["housekeeping"]
    items = {item["path"]: item for item in report["checkouts"]}
    own = items[os.path.realpath(started["data"]["worktree_path"])]
    assert own["status"] == "retained"
    old = items[os.path.realpath(leftover)]
    assert old["status"] == "reclaimable"
    assert old["detached"]
    assert report["reclaimable_bytes"] >= old["bytes"] > 0
    assert report["next_command"] == "agentic-preflight gc"
    assert leftover.exists()


def test_inventory_failure_leaves_start_ok(feature_repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(housekeeping, "inventory", boom)

    started = ScriptedAgent(feature_repo).run("start", "--intent", "prepare feature x")

    assert started["ok"] is True
    assert started["data"]["housekeeping"] == {
        "error": "housekeeping inventory failed (RuntimeError)"
    }
