"""Active gate ownership is scoped to one source worktree, not the clone."""

import json
import shlex
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path

import pytest

from agentic_preflight import runs
from agentic_preflight.envelope import ExitCode
from agentic_preflight.store import CurrentRunExists
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def test_commands_classify_and_preserve_unsupported_config_snapshot(feature_repo):
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    path = _state_root(feature_repo) / "runs" / started["run_id"] / "run.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["config_snapshot"]["review"]["require_fix_commits"] = True
    raw["config_snapshot"]["worktree"]["ttl_hours"] = 24
    original = json.dumps(raw).encode()
    path.write_bytes(original)

    expected_fields = [
        {
            "location": ["config_snapshot", "review", "require_fix_commits"],
            "category": "extra_forbidden",
        },
        {
            "location": ["config_snapshot", "worktree", "ttl_hours"],
            "category": "extra_forbidden",
        },
    ]
    status = agent.run("status")
    assert status["data"]["read_failure"]["reason"] == "invalid_or_unsupported_schema"
    assert status["data"]["read_failure"]["fields"] == expected_fields
    assert "review.require_fix_commits" in status["data"]["read_failure"]["diagnostic"]
    assert "worktree.ttl_hours" in status["data"]["read_failure"]["diagnostic"]

    inventory = agent.run("status", "--all")
    assert inventory["data"]["runs"][0]["fields"] == expected_fields
    collected = agent.run("gc")
    assert collected["data"]["retained"][0]["fields"] == expected_fields

    aborted = agent.run("abort", "--force", expect=ExitCode.PRECONDITION)
    assert aborted["error"]["code"] == "run_record_unreadable"
    assert aborted["data"]["reason"] == "invalid_or_unsupported_schema"
    assert aborted["data"]["fields"] == expected_fields
    assert path.read_bytes() == original


def _second_feature_worktree(feature_repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "feature-y"
    git("branch", "feature/y", "main", cwd=feature_repo)
    git("worktree", "add", str(path), "feature/y", cwd=feature_repo)
    write(path, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    commit_all(path, "change the greeting")
    return path


def _state_root(repo: Path) -> Path:
    return (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))
        / "agentic-preflight"
    )


def test_two_linked_worktrees_can_run_gates_independently(feature_repo, tmp_path):
    second_repo = _second_feature_worktree(feature_repo, tmp_path)
    first_agent = ScriptedAgent(feature_repo, transport="subprocess")
    second_agent = ScriptedAgent(second_repo, transport="subprocess")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_agent.run, "start", "--intent", "prepare feature x")
        second_future = pool.submit(second_agent.run, "start", "--intent", "prepare feature y")
        first = first_future.result(timeout=30)
        second = second_future.result(timeout=30)

    assert first["run_id"] != second["run_id"]
    assert first_agent.run("status")["run_id"] == first["run_id"]
    assert second_agent.run("status")["run_id"] == second["run_id"]
    assert first_agent.run("--run", second["run_id"], "status")["run_id"] == second["run_id"]

    first_agent.run("abort")

    assert second_agent.run("status")["run_id"] == second["run_id"]
    inventory = second_agent.run("status", "--all")["data"]
    assert {item["run_id"] for item in inventory["runs"]} >= {
        first["run_id"],
        second["run_id"],
    }
    assert second["run_id"] in inventory["active"].values()
    assert first["run_id"] not in inventory["active"].values()


def test_a_marked_stale_run_is_not_resumed_even_when_source_head_matches(feature_repo):
    from agentic_preflight import runs
    from agentic_preflight.machine import State

    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    session = runs.open_session(feature_repo)
    with session.store.transaction(started["run_id"]) as doc:
        doc.stale = True
    replacement = agent.run("start")
    assert replacement["run_id"] != started["run_id"]
    assert session.store.load_run(started["run_id"]).state is State.ORPHANED


def test_moving_the_source_head_orphans_the_stale_run_on_the_next_start(feature_repo, tmp_path):
    agent = ScriptedAgent(feature_repo)
    first = agent.run("start", "--intent", "prepare the original head")
    write(feature_repo, "src/extra.py", "value = 1\n")
    commit_all(feature_repo, "advance the source branch")

    second = agent.run("start", "--intent", "prepare the advanced head")

    assert second["run_id"] != first["run_id"]
    old = json.loads(
        (_state_root(feature_repo) / "runs" / first["run_id"] / "run.json").read_text()
    )
    assert old["state"] == "ORPHANED"
    assert old["orphaned_reason"] == "source worktree moved"


def test_restart_commands_preserve_a_non_default_base_ref(feature_repo):
    git("branch", "release", "main", cwd=feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start", "--base-ref", "release", "--intent", "first objective")

    refused = agent.run(
        "start",
        "--base-ref",
        "release",
        "--intent",
        "second objective",
        expect=ExitCode.PRECONDITION,
    )
    assert shlex.split(refused["next"]["command"]) == [
        "agentic-preflight",
        "start",
        "--replace",
        "--base-ref",
        "release",
        "--intent",
        "second objective",
    ]

    write(feature_repo, "src/extra.py", "value = 1\n")
    commit_all(feature_repo, "advance the source branch")
    stale = agent.run("status")
    assert shlex.split(stale["next"]["command"]) == [
        "agentic-preflight",
        "start",
        "--base-ref",
        "release",
        "--intent",
        "first objective",
    ]


def test_commands_from_an_isolated_validator_resolve_the_owning_run(feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation")
    source_agent = ScriptedAgent(feature_repo)
    started = source_agent.run("start")
    validator = Path(started["data"]["worktree_path"])

    validator_agent = ScriptedAgent(validator)
    from_validator = validator_agent.run("status")

    assert from_validator["run_id"] == started["run_id"]
    assert from_validator["data"]["source_worktree_path"] == str(feature_repo)
    refused = validator_agent.run("start", expect=ExitCode.PRECONDITION)
    assert refused["run_id"] == started["run_id"]
    assert refused["data"]["source_worktree_path"] == str(feature_repo)


def test_gc_orphans_an_abandoned_run_whose_ownership_pointer_disappeared(feature_repo):
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    state_root = _state_root(feature_repo)
    for pointer in (state_root / "active").glob("*.run"):
        if pointer.read_text(encoding="utf-8").strip() == started["run_id"]:
            pointer.unlink()

    collected = agent.run("gc")

    assert started["run_id"] in collected["data"]["removed"]
    old = json.loads((state_root / "runs" / started["run_id"] / "run.json").read_text())
    assert old["state"] == "ORPHANED"
    assert old["orphaned_reason"] == "source worktree lease disappeared"


def test_gc_does_not_orphan_a_run_while_start_claims_its_source_lease(feature_repo, monkeypatch):
    session = runs.open_session(feature_repo)
    original_claim = session.store.claim_active
    claim_started = threading.Event()
    allow_claim = threading.Event()

    def delayed_claim(owner_id, run_id):
        claim_started.set()
        if not allow_claim.wait(timeout=5):
            raise AssertionError("test did not release the delayed active-lease claim")
        original_claim(owner_id, run_id)

    monkeypatch.setattr(session.store, "claim_active", delayed_claim)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runs.start, session, intent="protect the creation window")
        try:
            assert claim_started.wait(timeout=5)
            collected = runs.gc(runs.open_session(feature_repo))
            assert collected.data["removed"] == []
            assert collected.data["retained"] == [
                {
                    "run_id": next(iter(session.store.list_runs())),
                    "reason": "run command is still executing",
                }
            ]
        finally:
            allow_claim.set()
        started = future.result(timeout=30)

    assert runs.status(runs.open_session(feature_repo)).run_id == started.run_id


def test_missing_source_worktree_blocks_mutation_but_allows_gc(feature_repo, tmp_path):
    source_repo = _second_feature_worktree(feature_repo, tmp_path)
    write(source_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(source_repo, "use strict validation")
    started = ScriptedAgent(source_repo).run("start", "--intent", "validate feature y")
    validator = Path(started["data"]["worktree_path"])
    git("worktree", "remove", "--force", str(source_repo), cwd=feature_repo)
    recovery_agent = ScriptedAgent(feature_repo)

    inspected = recovery_agent.run("--run", started["run_id"], "status")
    assert inspected["data"]["source_worktree_available"] is False
    assert inspected["next"]["command"] == "agentic-preflight gc"
    blocked = recovery_agent.run(
        "--run",
        started["run_id"],
        "mergeback",
        expect=ExitCode.PRECONDITION,
    )
    assert blocked["error"]["code"] == "source_worktree_missing"
    assert blocked["data"]["source_worktree_path"] == str(source_repo.resolve())
    assert validator.exists()

    collected = recovery_agent.run("--run", started["run_id"], "gc")
    assert started["run_id"] in collected["data"]["removed"]
    assert not validator.exists()


def test_alias_claim_failure_records_validator_for_gc(feature_repo, monkeypatch):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation")
    start_module = import_module("agentic_preflight.runs.start")

    def reject_alias(_session, _owner_id, _run_id):
        raise CurrentRunExists("r_conflicting")

    monkeypatch.setattr(start_module, "_claim_alias", reject_alias)
    agent = ScriptedAgent(feature_repo)
    failed = agent.run("start", expect=ExitCode.NEEDS_HUMAN)
    validator = Path(failed["data"]["worktree_path"])
    run = json.loads(
        (_state_root(feature_repo) / "runs" / failed["run_id"] / "run.json").read_text()
    )
    assert run["worktree_path"] == str(validator)
    assert validator.exists()

    collected = agent.run("gc")
    assert failed["run_id"] in collected["data"]["removed"]
    assert not validator.exists()


def test_replace_refuses_while_the_existing_run_is_executing(feature_repo):
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start", "--intent", "first objective")
    lock_path = _state_root(feature_repo) / "runs" / started["run_id"] / ".operation.lock"
    holder_script = textwrap.dedent(
        """
        import sys
        from agentic_preflight import filelock
        with filelock.exclusive(sys.argv[1]):
            print("locked", flush=True)
            sys.stdin.readline()
        """
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        refused = agent.run(
            "start",
            "--replace",
            "--intent",
            "different objective",
            expect=ExitCode.PRECONDITION,
        )
        assert "executing a command" in refused["error"]["message"]
        assert refused["run_id"] == started["run_id"]
    finally:
        holder.stdin.write("release\n")
        holder.stdin.flush()
        holder.wait(timeout=5)


def test_replace_preserves_an_isolated_validation_checkout(feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation")
    agent = ScriptedAgent(feature_repo)
    first = agent.run("start", "--intent", "first objective")
    first_validator = Path(first["data"]["worktree_path"])

    second = agent.run("start", "--replace", "--intent", "different objective")

    assert second["run_id"] != first["run_id"]
    assert first_validator.exists()
    old = json.loads(
        (_state_root(feature_repo) / "runs" / first["run_id"] / "run.json").read_text()
    )
    assert old["state"] == "ORPHANED"
    assert old["worktree_released"] is False


@pytest.mark.parametrize(
    "kind",
    [
        "unknown_field",
        "invalid_value",
        "malformed_json",
        "invalid_record",
        "invalid_encoding",
        "io_error",
    ],
)
def test_status_and_start_preserve_existing_unreadable_run(feature_repo, monkeypatch, kind):
    from tests.conftest import make_run, unreadable_run_bytes

    session = runs.open_session(feature_repo)
    store = session.store
    run = make_run()
    store.create_run(run)
    path = store.run_path(run.run_id)
    original = unreadable_run_bytes(run, kind)
    path.write_bytes(original)
    store.set_active(session.owner_id, run.run_id)
    store.set_active("second-alias", run.run_id)
    aliases = store.list_active()
    if kind == "io_error":
        read = Path.read_text

        def denied(record, *args, **kwargs):
            if record == path:
                raise PermissionError(13, "DO_NOT_ECHO_RECORD_VALUES")
            return read(record, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    agent = ScriptedAgent(feature_repo)
    for args in [("status",), ("--run", run.run_id, "status")]:
        result = agent.run(*args)
        assert result["run_id"] == run.run_id
        assert result["data"]["has_run"] is True
        assert result["data"]["readable"] is False
        assert result["next"]["command"] is None
        assert "compatible" in result["next"]["instruction"]
        assert "DO_NOT_ECHO_RECORD_VALUES" not in json.dumps(result)
    inventory = agent.run("status", "--all")
    assert inventory["data"]["runs"][0]["readable"] is False
    assert inventory["data"]["runs"][0]["active"] is True
    assert "corrupt" not in inventory["data"]["runs"][0]
    assert inventory["data"]["runs"][0]["reason"] == result["data"]["read_failure"]["reason"]
    for args in [("start",), ("abort",)]:
        failure = agent.run(*args, expect=3)
        assert failure["error"]["code"] == "run_record_unreadable"
        assert failure["next"]["command"] is None
    assert store.list_active() == aliases
    assert path.read_bytes() == original


@pytest.mark.parametrize("force", [False, True])
def test_gc_retains_earlier_release_record_without_offering_force(feature_repo, force):
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    path = _state_root(feature_repo) / "runs" / started["run_id"] / "run.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    collected = agent.run("gc", *(["--force"] if force else []))

    assert collected["data"]["retained"] == [
        {
            "run_id": started["run_id"],
            "path": str(path),
            "reason": "invalid_or_unsupported_schema",
            "diagnostic": (
                "Run record declares a schema version from an earlier release. The "
                "record and its associated work have been retained unchanged; use a "
                "compatible tool version or inspect the record."
            ),
            "fields": [
                {
                    "location": ["schema_version"],
                    "category": "value_error",
                }
            ],
        }
    ]
    assert collected["next"]["command"] is None
    assert path.exists()


@pytest.mark.parametrize(
    "schema_version",
    [pytest.param("missing", id="missing"), None, True, 2.0, "2", 1, 3],
)
def test_forced_gc_preserves_invalid_version_record_and_ownership(feature_repo, schema_version):
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    session = runs.open_session(feature_repo)
    store = session.store
    path = store.run_path(started["run_id"])
    raw = json.loads(path.read_text(encoding="utf-8"))
    if schema_version == "missing":
        raw.pop("schema_version")
    else:
        raw["schema_version"] = schema_version
    original = json.dumps(raw).encode()
    path.write_bytes(original)
    ownership = store.list_active()

    collected = agent.run("gc", "--force")

    assert collected["data"]["retained"][0]["reason"] == "invalid_or_unsupported_schema"
    assert store.list_active() == ownership
    assert path.read_bytes() == original


@pytest.mark.parametrize("command", ["status", "gc"])
def test_missing_pointer_recovery_does_not_clear_concurrent_new_owner(
    feature_repo, monkeypatch, command
):
    from agentic_preflight.store import Store
    from tests.conftest import make_run

    session = runs.open_session(feature_repo)
    store = session.store
    store.set_active(session.owner_id, "r_missing")
    replacement = make_run("r_replacement")
    replacement.source_worktree_id = session.owner_id
    replacement.source_worktree_path = str(feature_repo)
    clear = Store.clear_active_if

    def changed(current_store, owner, run_id):
        current_store.create_run(replacement)
        current_store.set_active(owner, replacement.run_id)
        return clear(current_store, owner, run_id)

    monkeypatch.setattr(Store, "clear_active_if", changed)
    ScriptedAgent(feature_repo).run(command)
    assert store.get_active(session.owner_id) == replacement.run_id


def test_missing_record_status_keeps_deliberate_dangling_recovery(feature_repo):
    session = runs.open_session(feature_repo)
    session.store.set_active(session.owner_id, "r_missing")
    result = ScriptedAgent(feature_repo).run("status")
    assert result["data"]["has_run"] is False
    assert result["data"]["dangling_run_id"] == "r_missing"
    assert session.store.get_active(session.owner_id) is None


@pytest.mark.parametrize("args", [("status",), ("status", "--all"), ("gc",)])
def test_programming_errors_do_not_clear_ownership(feature_repo, monkeypatch, args):
    from agentic_preflight.store import Store
    from tests.conftest import make_run

    session = runs.open_session(feature_repo)
    session.store.create_run(make_run())
    session.store.set_active(session.owner_id, "r_abc123")

    def bug(*args):
        raise RuntimeError("unexpected parser bug")

    monkeypatch.setattr(Store, "load_run", bug)
    result = ScriptedAgent(feature_repo).run(*args, expect=1)
    assert result["error"]["code"] == "internal_error"
    assert session.store.get_active(session.owner_id) == "r_abc123"


@pytest.mark.parametrize(
    "args", [("status",), ("status", "--all"), ("gc",), ("gc", "--force"), ("start",)]
)
def test_stray_current_pointer_is_ignored_and_unchanged(feature_repo, args):
    from tests.conftest import make_run

    session = runs.open_session(feature_repo)
    store = session.store
    run = make_run()
    store.create_run(run)
    path = store.run_path(run.run_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    original = json.dumps(raw).encode()
    path.write_bytes(original)
    current_path = store.root / "current"
    pointer = (run.run_id + "\n").encode()
    current_path.write_bytes(pointer)

    result = ScriptedAgent(feature_repo).run(*args)

    if args == ("status",):
        assert result["data"]["has_run"] is False
    elif args == ("status", "--all"):
        assert result["data"]["runs"][0]["active"] is False
    elif args[0] == "gc":
        assert result["data"]["retained"][0]["run_id"] == run.run_id
    else:
        assert result["run_id"] != run.run_id
    assert current_path.read_bytes() == pointer
    assert path.read_bytes() == original
