"""The event log's hash chain, and the events that record who acted on what basis."""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentic_preflight.cli import main
from agentic_preflight.envelope import ExitCode
from agentic_preflight.reviewers.orchestration import REPORT_PROMPT_LIMIT, Orchestrator
from agentic_preflight.runs.review_executor import reviewer_report
from agentic_preflight.store import CHAIN_KEY, Store
from tests.conftest import commit_all, configure_base, git, make_run, write
from tests.driver import PROTECTED_CONFIG, ScriptedAgent, findings_json

RUN = "r_abc123"


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "agentic-preflight")


def _lines(store: Store) -> list[bytes]:
    return [line for line in store.events_path(RUN).read_bytes().split(b"\n") if line.strip()]


def _rewrite(store: Store, lines: list[bytes]) -> None:
    store.events_path(RUN).write_bytes(b"\n".join(lines) + b"\n")


def _events_path(repo: Path, run_id: str) -> Path:
    return repo / ".git" / "agentic-preflight" / "runs" / run_id / "events.jsonl"


# -- the chain ---------------------------------------------------------------


def test_each_event_names_the_digest_of_the_line_before_it(store):
    store.create_run(make_run())
    store.append_event(RUN, {"event": "one"})
    store.append_event(RUN, {"event": "two"})

    lines = _lines(store)
    events = [json.loads(line) for line in lines]
    assert events[0][CHAIN_KEY] is None
    assert events[1][CHAIN_KEY] == hashlib.sha256(lines[0]).hexdigest()
    assert events[2][CHAIN_KEY] == hashlib.sha256(lines[1]).hexdigest()

    chain = store.verify_events(RUN)
    assert chain.status == "intact"
    assert (chain.lines, chain.chained_lines, chain.unchained_lines, chain.torn_lines) == (
        3,
        3,
        0,
        0,
    )
    assert chain.breaks == []
    assert chain.head_sha256 == hashlib.sha256(lines[2]).hexdigest()


def test_a_caller_cannot_supply_its_own_link(store):
    store.create_run(make_run())
    store.append_event(RUN, {"event": "one", CHAIN_KEY: "0" * 64})
    assert store.verify_events(RUN).status == "intact"


def test_a_missing_log_is_empty(store):
    chain = store.verify_events(RUN)
    assert chain.status == "empty"
    assert chain.head_sha256 is None


def test_an_edited_line_breaks_the_line_after_it(store):
    store.create_run(make_run())
    store.append_event(RUN, {"event": "finding_resolved", "action": "dismissed"})
    store.append_event(RUN, {"event": "two"})
    lines = _lines(store)
    lines[1] = lines[1].replace(b"dismissed", b"fixed")
    _rewrite(store, lines)

    chain = store.verify_events(RUN)
    assert chain.status == "broken"
    assert [(item.line, item.reason) for item in chain.breaks] == [(3, "prev_mismatch")]


def test_a_removed_line_breaks_the_line_after_it(store):
    store.create_run(make_run())
    store.append_event(RUN, {"event": "one"})
    store.append_event(RUN, {"event": "two"})
    lines = _lines(store)
    _rewrite(store, [lines[0], lines[2]])

    chain = store.verify_events(RUN)
    assert [(item.line, item.reason) for item in chain.breaks] == [(2, "prev_mismatch")]


def test_a_removed_first_line_is_a_break(store):
    store.create_run(make_run())
    store.append_event(RUN, {"event": "one"})
    _rewrite(store, _lines(store)[1:])

    chain = store.verify_events(RUN)
    assert [(item.line, item.reason) for item in chain.breaks] == [(1, "prev_mismatch")]


def test_an_inserted_unchained_line_is_a_break(store):
    store.create_run(make_run())
    store.append_event(RUN, {"event": "one"})
    lines = _lines(store)
    _rewrite(store, [*lines, b'{"event": "pushed"}'])

    chain = store.verify_events(RUN)
    assert [(item.line, item.reason) for item in chain.breaks] == [(3, "unchained_line")]


def test_a_torn_line_does_not_break_the_chain(store):
    store.create_run(make_run())
    store.append_event(RUN, {"event": "one"})
    with open(store.events_path(RUN), "ab") as handle:
        handle.write(b'{"event": "thr')
    store.append_event(RUN, {"event": "two"})

    chain = store.verify_events(RUN)
    assert chain.status == "intact"
    assert chain.torn_lines == 1
    assert json.loads(_lines(store)[-1])[CHAIN_KEY] == hashlib.sha256(b'{"event": "thr').hexdigest()


def test_a_log_from_an_earlier_release_is_unchained_then_intact(store):
    store.create_run(make_run())
    legacy = [b'{"at": "2026-01-01T00:00:00Z", "event": "run_created"}', b'{"event": "one"}']
    _rewrite(store, legacy)
    assert store.verify_events(RUN).status == "unchained"

    store.append_event(RUN, {"event": "two"})
    chain = store.verify_events(RUN)
    assert chain.status == "intact"
    assert (chain.unchained_lines, chain.chained_lines) == (2, 1)
    assert json.loads(_lines(store)[-1])[CHAIN_KEY] == hashlib.sha256(legacy[1]).hexdigest()


def test_a_log_stripped_of_every_link_reads_as_an_earlier_release(store):
    # A stated limit, pinned so that it changes on purpose: run schema 2 has no field
    # that says a log must be chained, so this cannot be told from an old log.
    store.create_run(make_run())
    store.append_event(RUN, {"event": "one"})
    stripped = []
    for line in _lines(store):
        event = json.loads(line)
        del event[CHAIN_KEY]
        stripped.append(json.dumps(event, sort_keys=True).encode())
    _rewrite(store, stripped)

    assert store.verify_events(RUN).status == "unchained"


def test_the_previous_line_is_found_beyond_one_read_block(store):
    store.create_run(make_run())
    store.append_event(RUN, {"event": "big", "text": "x" * 20_000})
    store.append_event(RUN, {"event": "after"})
    assert store.verify_events(RUN).status == "intact"


# -- the commands ------------------------------------------------------------


def test_events_verify_reports_an_intact_chain(agent, feature_repo):
    agent.run("start")

    plain = agent.run("events")["data"]
    assert "chain" not in plain

    chain = agent.run("events", "--verify")["data"]["chain"]
    assert chain["status"] == "intact"
    assert chain["breaks"] == []
    assert len(chain["head_sha256"]) == 64


def test_events_verify_refuses_a_broken_chain(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    path = _events_path(feature_repo, run_id)
    lines = path.read_bytes().split(b"\n")
    lines[0] = lines[0].replace(b"state_transition", b"state_transitioN")
    path.write_bytes(b"\n".join(lines))

    env = agent.run("events", "--verify", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "event_log_broken"
    assert env["data"]["chain"]["status"] == "broken"
    assert env["data"]["chain"]["breaks"][0] == {"line": 2, "reason": "prev_mismatch"}
    assert env["next"]["command"] == "agentic-preflight abort"
    # Reading the history stays possible, so a person can inspect what changed.
    assert agent.run("events")["data"]["count"] >= 2


def test_mergeback_refuses_a_broken_chain_and_writes_no_note(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    path = _events_path(feature_repo, run_id)
    lines = [line for line in path.read_bytes().split(b"\n") if line.strip()]
    path.write_bytes(b"\n".join([lines[0], *lines[2:]]) + b"\n")

    env = agent.run("mergeback", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "event_log_broken"
    assert env["state"] == "TEST_GREEN"
    assert git("for-each-ref", "refs/notes/agentic-preflight", cwd=feature_repo) == ""


# -- who acted, and on what basis --------------------------------------------


@pytest.fixture
def verified(feature_repo, bare_remote, tmp_path) -> ScriptedAgent:
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"
    return agent


def _named(agent: ScriptedAgent, name: str) -> list[dict]:
    return [event for event in agent.run("events")["data"]["events"] if event["event"] == name]


def test_gate_records_what_it_showed_without_the_token(verified):
    gate = verified.run("gate")

    (shown,) = _named(verified, "gate_minted")
    assert shown["sha"] == gate["data"]["commits"][-1]["sha"]
    assert shown["commits"] == [commit["sha"] for commit in gate["data"]["commits"]]
    assert shown["risk"] == gate["data"]["risk"]
    assert gate["data"]["token"] not in json.dumps(verified.run("events")["data"]["events"])


def test_push_records_the_stated_authorization_before_it_pushes(verified):
    token = verified.run("gate")["data"]["token"]

    verified.run("push", "--confirm", token, "--dry-run")
    assert _named(verified, "push_authorized") == []

    verified.run("push", "--confirm", token, "--authorization", "user_request")

    (authorized,) = _named(verified, "push_authorized")
    assert authorized["authorization"] == "user_request"
    assert authorized["authorization_source"] == "flag"
    names = [event["event"] for event in verified.run("events")["data"]["events"]]
    assert names.index("push_authorized") < names.index("pushed")
    assert verified.run("events", "--verify")["data"]["chain"]["status"] == "intact"


def test_push_without_a_stated_authorization_records_none(verified):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)

    (authorized,) = _named(verified, "push_authorized")
    assert authorized["authorization"] is None
    assert authorized["authorization_source"] is None


def test_push_refuses_an_unknown_authorization(verified):
    token = verified.run("gate")["data"]["token"]
    env = verified.run(
        "push", "--confirm", token, "--authorization", "because", expect=ExitCode.USAGE_ERROR
    )
    assert env["error"]["code"] == "usage_error"
    assert _named(verified, "push_authorized") == []


# -- what reviewed the change ------------------------------------------------

REPORT = {
    "type": "agentic_preflight_reviewer",
    "schema_version": 1,
    "provider": "claude",
    "model": "claude-sonnet-5",
    "effort": "high",
    "prompts": [{"phase": "discovery", "sha256": "a" * 64}],
}
REPORTED = {
    **{key: REPORT[key] for key in ("provider", "model", "effort", "prompts")},
    "prompts_omitted": 0,
}


def test_the_last_report_line_in_the_output_is_read():
    earlier = {**REPORT, "model": "earlier"}
    output = "\n".join(['{"findings": []}', json.dumps(earlier), "noise", json.dumps(REPORT)])
    assert reviewer_report(output) == (REPORTED, None)
    assert reviewer_report('{"findings": []}\n') is None


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"model": "a model name with prose in it"}, "invalid_name"),
        ({"model": ""}, "invalid_name"),
        ({"provider": 7}, "invalid_name"),
        ({"effort": None}, "invalid_name"),
        ({"prompts": "none"}, "invalid_prompts"),
        ({"prompts": [{"phase": "discovery", "sha256": "short"}]}, "invalid_prompts"),
        ({"prompts": [{"phase": "Discovery!", "sha256": "a" * 64}]}, "invalid_prompts"),
        (
            {"prompts": [{"phase": "discovery", "sha256": "a" * 64, "text": "the prompt"}]},
            "invalid_prompts",
        ),
        ({"prompts": [{"phase": "discovery", "sha256": "a" * 64}] * 65}, "too_many_prompts"),
        ({"prompts_omitted": -1}, "invalid_prompts"),
        ({"prompts_omitted": True}, "invalid_prompts"),
        ({"prompts_omitted": "many"}, "invalid_prompts"),
    ],
)
def test_a_malformed_report_is_ignored_whole_and_names_the_problem(change, problem):
    assert reviewer_report(json.dumps({**REPORT, **change})) == (None, problem)


def test_every_distinct_prompt_of_a_phase_is_reported_once(monkeypatch):
    monkeypatch.delenv("AP_REVIEWER_EVENTS_FILE", raising=False)
    runner = Orchestrator("claude", {}, 0, None)
    runner.note_prompt("discovery", "first chunk")
    runner.note_prompt("discovery", "first chunk")
    runner.note_prompt("discovery", "second chunk")
    runner.note_prompt("verification", "first chunk")

    prompts = runner.report()["prompts"]
    assert [prompt["phase"] for prompt in prompts] == ["discovery", "discovery", "verification"]
    assert prompts[0]["sha256"] == hashlib.sha256(b"first chunk").hexdigest()
    assert prompts[1]["sha256"] == hashlib.sha256(b"second chunk").hexdigest()
    assert reviewer_report(json.dumps(runner.report())).problem is None


def test_a_review_with_more_prompts_than_the_limit_still_reports(monkeypatch):
    monkeypatch.delenv("AP_REVIEWER_EVENTS_FILE", raising=False)
    runner = Orchestrator("codex", {}, 0, None)
    for number in range(REPORT_PROMPT_LIMIT + 6):
        runner.note_prompt("discovery", f"chunk {number}")

    claim = reviewer_report(json.dumps(runner.report()))

    assert claim.problem is None
    assert len(claim.report["prompts"]) == REPORT_PROMPT_LIMIT
    assert claim.report["prompts"][0]["sha256"] == hashlib.sha256(b"chunk 0").hexdigest()
    assert claim.report["prompts_omitted"] == 6


REVIEWER = """\
import json
import sys

data = json.load(sys.stdin)
json.dump(
    {"coverage": {"manifest": data["review_coverage"]["manifest"], "examined": "all"},
     "findings": []},
    sys.stdout,
)
sys.stdout.write("\\n")
if sys.argv[1] == "report":
    print(json.dumps(REPORT), file=sys.stderr)
if sys.argv[1] == "oversized":
    # Built from text: an integer past the interpreter's digit limit cannot be a literal.
    print('{"type": "agentic_preflight_reviewer", "prompts_omitted": ' + "9" * 5000 + "}",
          file=sys.stderr)
"""


def _configure_reviewer(repo: Path, mode: str) -> None:
    write(repo, "reviewer.py", f"REPORT = {REPORT!r}\n" + REVIEWER)
    command = shlex.join([sys.executable, "reviewer.py", mode])
    write(
        repo,
        ".agentic-preflight.toml",
        f"[review]\nexecutor = 'command'\ncommand = {command!r}\n\n[docs]\nenabled = false\n",
    )
    commit_all(repo, "configure the reviewer")


def test_review_run_records_the_reviewers_report(feature_repo):
    _configure_reviewer(feature_repo, "report")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    agent.run("review", "run")

    (reported,) = _named(agent, "reviewer_reported")
    assert {key: reported[key] for key in REPORTED} == REPORTED
    assert reported["source"] == "command_output"
    assert reported["stage"] == "review"


def test_review_run_records_nothing_for_a_reviewer_that_reports_nothing(feature_repo):
    _configure_reviewer(feature_repo, "silent")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    agent.run("review", "run")

    assert _named(agent, "reviewer_reported") == []
    assert _named(agent, "reviewer_report_ignored") == []


def test_review_run_records_that_a_malformed_report_was_ignored(feature_repo, monkeypatch):
    monkeypatch.setitem(REPORT, "model", "a model name with prose in it")
    _configure_reviewer(feature_repo, "report")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    agent.run("review", "run")

    assert _named(agent, "reviewer_reported") == []
    (ignored,) = _named(agent, "reviewer_report_ignored")
    assert ignored["reason"] == "invalid_name"
    assert "prose" not in json.dumps(agent.run("events")["data"]["events"])


def test_a_report_line_that_cannot_be_decoded_is_skipped():
    oversized = '{"type": "agentic_preflight_reviewer", "prompts_omitted": ' + "9" * 5000 + "}"
    nested = '{"type": "agentic_preflight_reviewer", "prompts": ' + "[" * 100_000 + "}"

    # json.loads raises a plain ValueError for the first and a RecursionError for the
    # second, not JSONDecodeError. Neither may escape, and an earlier report still counts.
    assert reviewer_report(oversized) is None
    assert reviewer_report(nested) is None
    assert reviewer_report("\n".join([json.dumps(REPORT), oversized])) == (REPORTED, None)


def test_review_run_accepts_findings_beside_a_report_it_cannot_decode(feature_repo):
    _configure_reviewer(feature_repo, "oversized")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    env = agent.run("review", "run")

    assert env["state"] == "DOCS_GREEN"
    assert _named(agent, "reviewer_reported") == []
    assert _named(agent, "reviewer_report_ignored") == []
    assert len(_named(agent, "findings_submitted")) == 1


def test_the_packaged_reviewer_reports_its_model_and_prompt_digest(tmp_path, monkeypatch):
    fake = tmp_path / "provider.py"
    prompts = tmp_path / "prompt.txt"
    fake.write_text(
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(prompts)!r}).write_bytes(sys.stdin.buffer.read())\n"
        'print(json.dumps({"type": "result", "subtype": "success",'
        ' "result": json.dumps({"findings": []})}))\n',
        encoding="utf-8",
    )
    executable = fake
    if sys.platform == "win32":
        executable = tmp_path / "provider.cmd"
        executable.write_text(f'@"{sys.executable}" "{fake}" %*\n')
    else:
        fake.write_text(f"#!{sys.executable}\n" + fake.read_text(encoding="utf-8"))
        fake.chmod(0o755)
    context = {
        "diff": "+ changed()",
        "review_coverage": {"manifest": "a" * 64, "units": [{"id": "U0001", "path": "app.py"}]},
    }

    result = CliRunner().invoke(
        main,
        ["reviewer", "run", "--provider", "claude"],
        input=json.dumps(context),
        env={
            "AP_CLAUDE_BIN": str(executable),
            "AP_REVIEWER_MODEL": "claude-test-model",
            "AP_REVIEWER_EFFORT": "low",
            "AP_REVIEWER_TIMEOUT": "30",
        },
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["findings"] == []
    claim = reviewer_report(result.stderr)
    assert claim is not None
    report = claim.report
    assert report is not None
    assert (report["provider"], report["model"], report["effort"]) == (
        "claude",
        "claude-test-model",
        "low",
    )
    (prompt,) = report["prompts"]
    assert prompt["phase"]
    assert prompt["sha256"] == hashlib.sha256(prompts.read_bytes()).hexdigest()
