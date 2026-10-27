"""The typed agent contract: per-command ``data`` schemas and their documentation.

``agentic_preflight/cli_contract.py`` holds a pydantic model per command;
``tools/generate_schema.py`` renders them into ``skill/reference/schema/``. These tests
keep the generated files current, keep every command covered, and cross-check
``skill/reference/commands.md`` against the schemas.

Field extraction rule for commands.md. The reference is split into ``### `agentic-preflight
<command> ...` `` sections. Within a section:

* a **documented field** is the first segment after ``data.`` in a backticked span, for
  example ``data.sync.merged`` documents ``sync``;
* a schema property counts as **mentioned** when its name appears in any backticked span
  of the section, as ``data.<name>``, as a bare identifier, or as the first segment of a
  dotted path.

Every documented field must be a top-level property of that command's schema, and every
property should be mentioned. Known exceptions are listed below and must stay accurate:
an allowlist entry that no longer applies fails the test.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import click
import pytest

from agentic_preflight.cli import main
from agentic_preflight.cli_contract import (
    COMMAND_MODELS,
    NO_ENVELOPE,
    EnvelopeModel,
    RunDataBase,
    schema_filename,
    schemas,
)
from agentic_preflight.envelope import Envelope, ExitCode
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent, findings_json

ROOT = Path(__file__).parent.parent
SCHEMA_DIR = ROOT / "skill" / "reference" / "schema"
COMMANDS_MD = ROOT / "skill" / "reference" / "commands.md"

# Documented but not emitted: commands.md names `data.<field>` inside this command's
# section, but this command never emits it. Each describes another command's data.
DOCUMENTED_NOT_EMITTED: dict[str, set[str]] = {
    # documented but not emitted: the start section describes `status`, not `start`.
    "start": {"interrupted_legacy_update"},
    # documented but not emitted: the push section describes the gate envelope's `data.token`.
    "push": {"token"},
    # documented but not emitted: the gc section describes single-run `status`.
    "gc": {"read_failure"},
    # documented but not emitted: the telemetry rebuild section describes `finish`, `abort`, and `gc`.
    "telemetry rebuild": {"telemetry"},
}

# Emitted but undocumented: properties the command emits in at least one outcome that
# its commands.md section never mentions (shared run keys excluded).
EMITTED_NOT_DOCUMENTED: dict[str, set[str]] = {
    # emitted but undocumented
    "abort": {
        "discarded_fix_commits",
        "fix_commits",
        "preserved_in_place_commits",
        "worktree_path",
    },
    # emitted but undocumented
    "approval-check": {
        "approval_environment",
        "approval_mode",
        "base_sha",
        "environment_approved",
        "head_sha",
        "manual_merge_required",
        "notes_ref",
        "path_reasons",
        "reason",
        "requires_human_approval",
        "risk_level",
        "severe_findings",
        "sha",
    },
    # emitted but undocumented
    "context": {
        "base",
        "by_file",
        "coverage_invalidated",
        "diff_bytes",
        "intent",
        "intent_source",
        "max_bytes",
        "require_changelog",
        "risk",
        "section",
        "total_bytes",
    },
    # emitted but undocumented
    "events": {"count", "events"},
    # emitted but undocumented
    "finish": {"automated_cleanup", "base_ref", "branch", "pr_mode", "pushed_sha"},
    # emitted but undocumented
    "gate": {
        "approval_mode",
        "base_ref",
        "branch",
        "commits",
        "pr_mode",
        "push_commands",
        "refspec",
        "remote",
        "risk",
        "token",
    },
    # emitted but undocumented
    "gc": {"active", "runs_known"},
    # emitted but undocumented
    "init": {"config_path", "config_written", "repo_root", "worktree_mode"},
    # emitted but undocumented
    "integrations install": {"integrations", "scope"},
    # emitted but undocumented
    "integrations status": {"integrations", "scope"},
    # emitted but undocumented
    "integrations uninstall": {"integrations", "scope"},
    # emitted but undocumented
    "integrations update": {"integrations", "scope"},
    # emitted but undocumented
    "logs": {"log_path", "output", "stage"},
    # emitted but undocumented
    "mergeback": {
        "affected_paths",
        "applied",
        "base_ref",
        "base_sha",
        "branch_restored",
        "conflicting_commit",
        "conflicting_files",
        "coverage_invalidated",
        "fix_commits",
        "green_transferred",
        "inputs_invalidated",
        "local_tree_sha",
        "mode",
        "overlapping_status",
        "post_sha",
        "pre_sha",
        "tree_equivalent",
        "validation_restarted",
        "worktree_mode",
        "worktree_path",
        "worktree_tree_sha",
    },
    # emitted but undocumented
    "push": {
        "automated_cleanup",
        "base_ref",
        "branch",
        "dry_run",
        "pr_mode",
        "pushed",
        "remote",
        "sha",
        "would_push",
    },
    # emitted but undocumented
    "report": {"format", "rows_reported"},
    # emitted but undocumented
    "respond": {"coverage_invalidated", "finding", "remaining_blocking", "risk"},
    # emitted but undocumented
    "review compare": {
        "agreement_rate",
        "executors",
        "exit_code",
        "findings",
        "head_sha",
        "log_path",
        "manifest",
        "units",
    },
    # emitted but undocumented
    "review run": {
        "accepted",
        "attempts",
        "contract_checks",
        "copied_file",
        "copied_files",
        "coverage",
        "executor",
        "exit_code",
        "log_path",
        "mode",
        "output_head",
        "output_sha256",
        "output_tail",
        "phase",
        "reviewer_invocations",
        "risk",
        "stage",
        "timed_out",
        "total",
        "total_lines",
        "truncated",
    },
    # emitted but undocumented
    "stage run": {
        "attempts",
        "baseline_red",
        "copied_file",
        "copied_files",
        "coverage_invalidated",
        "exit_code",
        "log_path",
        "output_head",
        "output_tail",
        "setup_failure",
        "timed_out",
        "total_lines",
        "validation_restarted",
    },
    # emitted but undocumented
    "start": {
        "changed_files",
        "conflicting_run_id",
        "copied_files",
        "existing_intent",
        "head_sha",
        "intent",
        "intent_source",
        "merge_base_sha",
        "requested_intent",
        "resume_reason",
        "resumed",
        "risk",
        "source_head_sha",
        "worktree_branch",
        "worktree_mode",
    },
    # emitted but undocumented
    "status": {
        "applicability",
        "approval_mode",
        "automated_cleanup",
        "base_ref",
        "branch",
        "config_digest",
        "copied_files",
        "count",
        "current_tip",
        "dangling_run_id",
        "findings",
        "findings_summary",
        "fix_commits",
        "gate_token",
        "has_run",
        "head_sha",
        "intent",
        "intent_source",
        "interrupted_legacy_update",
        "mergeback_attempt",
        "mergeback_conflict",
        "owner_ids",
        "pr_mode",
        "previous_run_id",
        "previous_state",
        "pushed_sha",
        "read_failure",
        "reuse_error",
        "review_coverage",
        "risk",
        "runs",
        "seq",
        "setup_failure",
        "source_head_sha",
        "source_worktree_available",
        "source_worktree_id",
        "stages",
        "stale",
        "sync",
        "worktree_branch",
        "worktree_mode",
        "worktree_path",
        "worktree_released",
    },
    # emitted but undocumented
    "submit-findings": {"accepted", "coverage", "mode", "risk", "total"},
    # emitted but undocumented
    "verify": {
        "attestation",
        "blocking_count",
        "coverage_invalidated",
        "merge_requirements_satisfied",
        "notes_ref",
        "purpose",
        "reason",
        "sha",
        "stage",
        "test_status",
        "tree_sha",
    },
}


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_schema", ROOT / "tools" / "generate_schema.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _leaf_commands(group: click.Group, prefix: str = "") -> set[str]:
    names: set[str] = set()
    for name, cmd in group.commands.items():
        path = f"{prefix}{name}"
        if isinstance(cmd, click.Group):
            names |= _leaf_commands(cmd, f"{path} ")
        else:
            names.add(path)
    return names


_HEADING = re.compile(r"^### `agentic-preflight ([^`]*)`")


def _sections() -> dict[str, list[tuple[int, str]]]:
    """Map each command to its (line number, text) lines in commands.md."""
    commands = sorted(COMMAND_MODELS, key=len, reverse=True)
    sections: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    for number, line in enumerate(COMMANDS_MD.read_text(encoding="utf-8").splitlines(), 1):
        heading = _HEADING.match(line)
        if heading:
            usage = heading.group(1)
            current = next(
                (c for c in commands if usage == c or usage.startswith(c + " ")),
                None,
            )
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is not None:
            sections.setdefault(current, []).append((number, line))
    return sections


def _spans(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(n, span) for n, line in lines for span in re.findall(r"`([^`]+)`", line)]


def documented_fields(lines: list[tuple[int, str]]) -> dict[str, int]:
    found: dict[str, int] = {}
    for number, span in _spans(lines):
        match = re.match(r"data\.([a-z_]+)", span)
        if match:
            found.setdefault(match.group(1), number)
    return found


def mentioned_names(lines: list[tuple[int, str]]) -> set[str]:
    names: set[str] = set()
    for _, span in _spans(lines):
        span = span.removeprefix("data.")
        match = re.match(r"([a-z_]+)(?=[.\s=]|$)", span)
        if match:
            names.add(match.group(1))
    return names


def properties(command: str) -> set[str]:
    return set(COMMAND_MODELS[command].model_json_schema()["properties"])


def test_schema_files_are_current() -> None:
    render = _load_generator().render
    expected = {name: render(schema) for name, schema in schemas().items()}
    actual = {path.name: path.read_text(encoding="utf-8") for path in SCHEMA_DIR.glob("*.json")}
    assert actual == expected, "run `uv run python tools/generate_schema.py`"


def test_every_command_has_a_model() -> None:
    assert _leaf_commands(main) - NO_ENVELOPE == set(COMMAND_MODELS)
    for command in COMMAND_MODELS:
        assert (SCHEMA_DIR / schema_filename(command)).is_file()


def test_envelope_schema_matches_payload_keys() -> None:
    schema = json.loads((SCHEMA_DIR / "envelope.json").read_text(encoding="utf-8"))
    payload = Envelope().to_payload()
    assert set(schema["properties"]) == set(payload)
    assert set(schema["required"]) == set(payload)


def test_every_command_has_a_reference_section() -> None:
    assert set(_sections()) == set(COMMAND_MODELS)


@pytest.mark.parametrize("command", sorted(COMMAND_MODELS))
def test_documented_fields_exist_in_schema(command: str) -> None:
    documented = documented_fields(_sections()[command])
    missing = {
        field: line for field, line in documented.items() if field not in properties(command)
    }
    allowed = DOCUMENTED_NOT_EMITTED.get(command, set())
    assert set(missing) == allowed, f"{command}: documented but not in schema: {missing}"


@pytest.mark.parametrize("command", sorted(COMMAND_MODELS))
def test_emitted_fields_are_documented(command: str) -> None:
    # Keys every run command may carry (restart limit, telemetry) are documented once,
    # under `stage run` and `telemetry rebuild`, not in every section.
    shared = set(RunDataBase.model_fields)
    undocumented = properties(command) - shared - mentioned_names(_sections()[command])
    assert undocumented == EMITTED_NOT_DOCUMENTED.get(command, set())


class _RecordingAgent(ScriptedAgent):
    """A scripted agent that keeps every envelope it receives."""

    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.captured: list[tuple[str, dict]] = []

    def run(self, *argv: str, expect: int = 0) -> dict:
        payload = super().run(*argv, expect=expect)
        words = [a for a in argv if not a.startswith("-")]
        command = next(
            c
            for c in sorted(COMMAND_MODELS, key=len, reverse=True)
            if words[: len(c.split())] == c.split()
        )
        self.captured.append((command, payload))
        return payload


def test_real_envelopes_match_the_models(feature_repo, bare_remote, tmp_path) -> None:
    """Drive a real run and check every captured `data` against its command's model.

    The models allow extra keys, so the top-level keys are checked explicitly. Nested
    open-ended values are typed ``dict[str, Any]`` and are not inspected further.
    """
    agent = _RecordingAgent(feature_repo)
    agent.run("status")  # no run yet
    agent.run("finish", expect=ExitCode.PRECONDITION)  # error envelope
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[worktree]\nmode = 'reusable'\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")
    agent.run("start")
    agent.run("gate", expect=ExitCode.PRECONDITION)  # error envelope
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("logs", "--stage", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    agent.run("status")
    agent.run("status", "--all")
    agent.run("events")
    token = agent.run("gate")["data"]["token"]
    agent.run("push", "--confirm", token, "--dry-run")
    agent.run("push", "--confirm", token)
    agent.run("finish")
    agent.run("status")
    agent.run("gc")

    seen = {command for command, _ in agent.captured}
    assert seen >= {"status", "finish", "start", "gate", "context", "submit-findings"}
    assert seen >= {"stage run", "logs", "mergeback", "events", "push", "gc"}
    for command, payload in agent.captured:
        model = COMMAND_MODELS[command]
        data = payload["data"]
        extra = set(data) - set(model.model_fields)
        assert not extra, f"{command}: keys missing from {model.__name__}: {sorted(extra)}"
        model.model_validate(data)
        EnvelopeModel.model_validate(payload)
