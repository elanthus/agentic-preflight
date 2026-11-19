"""The ``audit`` command: one run's audit trail as a self-contained HTML report."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_preflight import attestation, gitx
from agentic_preflight.cli_contract import AuditData
from agentic_preflight.envelope import ExitCode
from agentic_preflight.machine import State
from agentic_preflight.models import Stage
from agentic_preflight.public_config import REDACTED_COMMAND
from agentic_preflight.report import audit as audit_data
from agentic_preflight.report.audit import WITHHELD_CONFIG, build_audit
from agentic_preflight.report.audit_html import _duration as render_duration
from agentic_preflight.report.audit_html import render
from agentic_preflight.runs import open_session, respond
from agentic_preflight.runs.audit import audit as run_audit
from agentic_preflight.store import Store, StoreError
from tests.conftest import commit_all, configure_base, git, write
from tests.driver import PROTECTED_CONFIG, ScriptedAgent, findings_json

#: The command's module; ``agentic_preflight.runs.audit`` is the command function.
run_audit_module = importlib.import_module("agentic_preflight.runs.audit")

# -- a realistic sample run --------------------------------------------------

REVIEWER = '''\
"""A scripted stand-in reviewer for the sample run."""
import json
import sys
from pathlib import Path

data = json.load(sys.stdin)
findings = []
if "if quantity <= 0" not in Path("src/pricing.py").read_text(encoding="utf-8"):
    findings = [
        {
            "path": "src/pricing.py",
            "line": 2,
            "severity": "high",
            "action": "auto_fix",
            "title": "Division by zero when quantity is 0",
            "detail": "unit_price divides by quantity without a guard, so an empty order "
            "raises ZeroDivisionError.",
            "suggestion": "Reject a quantity below 1 before dividing.",
        },
        {
            "path": "src/pricing.py",
            "line": 5,
            "severity": "low",
            "action": "no_op",
            "title": "Consider Decimal for currency amounts",
            "detail": "Floating point can round currency values.",
        },
    ]
coverage = {"manifest": data["review_coverage"]["manifest"], "examined": "all"}
json.dump({"coverage": coverage, "findings": findings}, sys.stdout)
sys.stdout.write("\\n")
report = {
    "type": "agentic_preflight_reviewer",
    "schema_version": 1,
    "provider": "claude",
    "model": "claude-sonnet-5",
    "effort": "high",
    "prompts": [{"phase": "discovery", "sha256": "3f" * 32}],
}
print(json.dumps(report), file=sys.stderr)
'''

SAMPLE_CONFIG = """\
[review]
executor = "command"
command = "python tools/reviewer.py"

[docs]
enabled = false

[commands]
lint = "python -B -m tabnanny src"
test = "python -B -m unittest discover -s tests -t ."

[records]
publish = true
"""

PRICING = "def unit_price(total, quantity):\n    return total / quantity\n"
DISCOUNT = (
    "\n\ndef bulk_discount(total, quantity):\n"
    "    return unit_price(total, quantity) * (0.9 if quantity >= 10 else 1)\n"
)
GUARDED = (
    "def unit_price(total, quantity):\n"
    "    if quantity <= 0:\n"
    '        raise ValueError("quantity must be positive")\n'
    "    return total / quantity\n"
)
UNIT_TEST = """\
import unittest

from src.pricing import unit_price


class UnitPrice(unittest.TestCase):
    def test_divides(self):
        self.assertEqual(unit_price(10, 4), 2.5)
"""
INTENT = (
    "Add a 10% bulk discount for orders of ten or more units, without changing single-unit prices."
)


def _sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample-service"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    write(repo, "src/__init__.py", "")
    write(repo, "src/pricing.py", PRICING)
    write(repo, "tests/__init__.py", "")
    write(repo, "tests/test_pricing.py", UNIT_TEST)
    write(repo, "tools/reviewer.py", REVIEWER)
    write(repo, ".agentic-preflight.toml", SAMPLE_CONFIG)
    commit_all(repo, "Add unit pricing")
    remote = tmp_path / "remote" / "sample-service.git"
    remote.parent.mkdir()
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "-u", "origin", "main", cwd=repo)
    git("switch", "-c", "feature/bulk-discount", cwd=repo)
    write(repo, "src/pricing.py", PRICING + DISCOUNT)
    commit_all(repo, "Add a bulk discount")
    return repo


@pytest.fixture
def sample(tmp_path) -> tuple[ScriptedAgent, Path, str]:
    """A pushed run with a reviewer report, a fixed and a dismissed finding."""
    repo = _sample_repo(tmp_path)
    agent = ScriptedAgent(repo)
    started = agent.run(
        "start", "--intent", INTENT, "--client", "claude-code", "--model", "claude-opus-5"
    )
    run_id = started["run_id"]
    assert agent.run("review", "run")["state"] == "REVIEW_BLOCKED"
    worktree = Path(agent.run("status")["data"]["worktree_path"])
    write(worktree, "src/pricing.py", GUARDED + DISCOUNT)
    git("commit", "-am", "Reject a non-positive quantity in unit_price", cwd=worktree)
    fix = git("rev-parse", "HEAD", cwd=worktree)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)
    agent.run(
        "respond",
        "--id",
        "F002",
        "--action",
        "dismissed",
        "--reason",
        "out_of_scope",
        "--note",
        "Prices are stored in integer cents elsewhere; this function only divides them.",
    )
    agent.run("verify")
    agent.run("review", "run")
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"
    token = agent.run("gate")["data"]["token"]
    agent.run("push", "--confirm", token, "--authorization", "user_request")
    agent.run("finish")
    return agent, repo, run_id


def _events_bytes(repo: Path, run_id: str) -> bytes:
    return (repo / ".git" / "agentic-preflight" / "runs" / run_id / "events.jsonl").read_bytes()


def test_the_audit_data_answers_what_happened_and_who_acted(sample):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    data = build_audit(session, session.store.load_run(run_id))

    assert data["summary"]["state"] == "DONE"
    assert data["summary"]["chain_status"] == "intact"
    assert data["summary"]["findings"] == {"open": 0, "fixed": 1, "dismissed": 1, "accepted": 0}
    assert data["run"]["intent"] == INTENT
    assert data["run"]["repository"] == "sample-service"

    producer = {item["label"]: item for item in data["actors"]["producer"]["items"]}
    assert producer["Agentic Preflight version"]["kind"] == "measured"
    assert producer["Coding agent"] == {
        "label": "Coding agent",
        "value": "claude-code",
        "kind": "claimed",
        "source": "flag",
    }
    assert producer["Agent's model"]["kind"] == "claimed"

    (report, *_) = data["actors"]["reviewer"]["reports"]
    assert {item["label"]: item["value"] for item in report["items"]} == {
        "Provider": "claude",
        "Model": "claude-sonnet-5",
        "Effort": "high",
    }
    assert all(item["kind"] == "claimed" for item in report["items"])
    assert data["actors"]["reviewer"]["executor"]["value"] == "command"
    (authorized,) = data["actors"]["authorizations"]
    assert authorized["claim"]["value"] == "user_request"
    assert authorized["claim"]["kind"] == "claimed"

    findings = {item["id"]: item for item in data["findings"]["items"]}
    assert findings["F001"]["fix_subject"] == "Reject a non-positive quantity in unit_price"
    assert findings["F002"]["reason"] == "out_of_scope"
    assert findings["F002"]["note"].startswith("Prices are stored")

    stages = {stage["name"]: stage for stage in data["stages"]}
    assert stages["lint"]["status"] == "green"
    assert stages["lint"]["command"] == "python -B -m tabnanny src"
    assert stages["docs"]["status"] == "skipped"

    publication = data["publication"]
    assert publication["attestation"]["valid"] is True
    assert publication["attestation"]["same_run"] is True
    (gate,) = publication["gates"]
    assert gate["commits"][-1]["sha"] == publication["pushes"][0]["sha"]
    assert [event["event"] for event in data["timeline"]["events"]][-1] == "finished"


def test_audit_writes_the_report_and_changes_nothing(sample):
    agent, repo, run_id = sample
    store = Store(repo / ".git" / "agentic-preflight")
    events_before = _events_bytes(repo, run_id)
    record_before = store.run_path(run_id).read_bytes()

    env = agent.run("audit")

    expected = store.run_dir(run_id) / "audit.html"
    assert env["ok"] is True
    assert env["run_id"] == run_id
    assert env["state"] == "DONE"
    assert env["data"]["path"] == str(expected)
    assert env["data"]["size_bytes"] == expected.stat().st_size
    assert env["data"]["chain_status"] == "intact"
    assert env["data"]["findings_by_status"]["fixed"] == 1
    assert env["data"]["attested"] is True
    assert env["data"]["note_verified"] is True
    assert env["data"]["local_details"] is False
    assert env["data"]["run_selection"] == "most_recent"
    assert _events_bytes(repo, run_id) == events_before
    assert store.run_path(run_id).read_bytes() == record_before

    text = expected.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")
    for heading in ("What this record proves", "Who acted", "Stages", "Findings", "Timeline"):
        assert heading in text
    assert "Self-reported" in text
    assert "Event log links intact" in text
    assert "does not show that the log is complete" in text


def test_audit_selects_a_finished_run_with_the_global_run_option(sample, tmp_path):
    agent, _repo, run_id = sample
    assert agent.run("audit")["data"]["run_selection"] == "most_recent"

    out = tmp_path / "out" / "report.html"
    env = agent.run("--run", run_id, "audit", "--output", str(out))

    assert env["run_id"] == run_id
    assert env["data"]["run_selection"] == "selected"
    assert env["data"]["path"] == str(out.resolve())
    assert out.is_file()


def test_the_report_shows_the_note_anchor_and_the_records_bundle(sample):
    agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    data = build_audit(session, session.store.load_run(run_id))

    # The run's own note pins the part of the log written before merge-back.
    chain = data["chain"]
    assert chain["status"] == "intact"
    assert chain["banner"]["headline"] == "Event log links intact and anchored"
    assert 0 < chain["anchor"]["lines"] < chain["lines"]
    note = data["publication"]["attestation"]
    assert note["same_run"] is True
    assert note["schema_version"] == 8
    assert note["events"] == chain["anchor"]
    assert note["producer_recorded"] is True
    assert note["transcripts"] == {}

    # The sample repository opts in to the records bundle, and it matches the note.
    records = note["records"]
    assert records["checked"] is True
    assert records["error"] is None
    assert {"intent.txt", "events.jsonl", "logs/lint.txt", "logs/test.txt"} <= set(records["files"])

    text = Path(agent.run("audit")["data"]["path"]).read_text(encoding="utf-8")
    assert "Event log links intact and anchored" in text
    assert "Matches the note" in text
    assert records["commit"][:12] in text


def test_a_note_that_parses_but_does_not_verify_is_not_shown_as_verified(sample):
    agent, repo, run_id = sample
    assert agent.run("--run", run_id, "audit")["data"]["note_verified"] is True

    # A well-formed note that names another tree: it still reads, and must not verify.
    head = git("rev-parse", "HEAD", cwd=repo)
    value = attestation.read(repo, head)
    assert value is not None
    forged = value.model_copy(update={"tree_sha": "0" * 40})
    gitx.write_note(repo, attestation.NOTES_REF, head, attestation.encode(forged))

    envelope = agent.run("--run", run_id, "audit")

    assert envelope["data"]["attested"] is True
    assert envelope["data"]["note_verified"] is False
    text = Path(envelope["data"]["path"]).read_text(encoding="utf-8")
    assert "Does not verify" in text
    assert "tree_mismatch" in text
    assert "Verifies in this clone" not in text


def test_a_note_that_cannot_be_read_is_reported_as_not_verified(sample):
    agent, repo, run_id = sample
    head = git("rev-parse", "HEAD", cwd=repo)
    gitx.write_note(repo, attestation.NOTES_REF, head, "this is not an attestation")

    envelope = agent.run("--run", run_id, "audit")

    # Present but malformed: not a well-formed note, and a failed check, not a missing one.
    assert envelope["data"]["attested"] is False
    assert envelope["data"]["note_verified"] is False
    text = Path(envelope["data"]["path"]).read_text(encoding="utf-8")
    assert "The note is present but invalid" in text


def test_the_chain_check_and_the_shown_note_come_from_one_read(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)
    real = attestation.read
    reads = []

    def counted(read_repo, sha):
        reads.append(sha)
        return real(read_repo, sha)

    monkeypatch.setattr(attestation, "read", counted)
    data = build_audit(session, run)

    # One read: the notes ref cannot move between the anchor check and the note shown.
    assert reads == [run.pushed_sha]
    assert data["chain"]["anchor"] == data["publication"]["attestation"]["events"]
    assert data["chain"]["anchor_error"] is None


def test_a_notes_ref_that_cannot_be_read_is_reported_as_an_unchecked_anchor(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)

    def unreadable(*_args, **_kwargs):
        raise gitx.GitError(["notes", "show"], 128, "fatal: cannot read /opt/git/notes-tree")

    monkeypatch.setattr(attestation, "read", unreadable)
    data = build_audit(session, run)

    chain = data["chain"]
    assert chain["status"] == "intact"
    assert chain["anchor"] is None
    assert "the notes ref could not be read" in chain["anchor_error"]
    # Not the banner of a run without a note: the comparison could not be made.
    assert chain["banner"]["tone"] == "warn"
    assert chain["banner"]["headline"] == "Event log links intact; anchor not checked"
    assert "was not compared with the anchor" in chain["banner"]["detail"]
    assert data["publication"]["attestation"]["present"] is None
    # The error can name a local path, and the shareable view withholds it.
    assert "/opt/git" not in json.dumps(data)
    assert "anchor not checked" in render(data)


def test_a_log_rewritten_before_the_note_is_reported_as_an_anchor_mismatch(sample):
    agent, repo, run_id = sample
    path = repo / ".git" / "agentic-preflight" / "runs" / run_id / "events.jsonl"
    lines = [line for line in path.read_bytes().split(b"\n") if line.strip()]
    # Drop one early line and re-link every later line, so the chain alone passes.
    kept = [lines[0], *lines[2:]]
    relinked = [kept[0]]
    for raw in kept[1:]:
        event = json.loads(raw)
        event["prev_sha256"] = hashlib.sha256(relinked[-1]).hexdigest()
        relinked.append(json.dumps(event, sort_keys=True).encode())
    path.write_bytes(b"\n".join(relinked) + b"\n")

    envelope = agent.run("--run", run_id, "audit")

    assert envelope["data"]["chain_status"] == "broken"
    text = Path(envelope["data"]["path"]).read_text(encoding="utf-8")
    assert "Event log broken" in text
    assert "anchor_mismatch" in text


def test_the_report_is_self_contained(sample, tmp_path):
    agent, _repo, _run_id = sample
    path = Path(agent.run("audit")["data"]["path"])
    text = path.read_text(encoding="utf-8")

    assert "<script" not in text.lower()
    assert "<link" not in text.lower()
    assert "<img" not in text.lower()
    assert "url(" not in text
    assert "@import" not in text
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:|//)""", text)
    assert "default-src 'none'" in text
    assert "prefers-color-scheme:dark" in text
    assert "@media print" in text


# -- untrusted text ----------------------------------------------------------

HOSTILE_TITLE = '<script>alert("x")</script>" onmouseover="alert(1)" x=\''


def test_untrusted_text_cannot_produce_markup(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    agent.run("start", "--intent", "</blockquote><img src=x onerror=alert(1)>")
    agent.run("context")
    finding = {
        "path": "src/app.py",
        "line": 1,
        "severity": "low",
        "action": "no_op",
        "title": HOSTILE_TITLE,
        "detail": "<b>bold</b> & <a href='javascript:alert(1)'>link</a>",
    }
    agent.run("submit-findings", "--file", findings_json(tmp_path, [finding]))
    agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--reason",
        "other",
        "--note",
        '"><svg onload=alert(1)>',
    )

    text = Path(agent.run("audit")["data"]["path"]).read_text(encoding="utf-8")

    lowered = text.lower()
    assert "<script" not in lowered
    assert "<img" not in lowered
    assert "<svg" not in lowered
    assert "<b>" not in lowered
    assert "<a href='javascript" not in lowered
    assert '" onmouseover="' not in text
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&quot; onmouseover=" in text
    # Every attribute value in the document is one the renderer wrote.
    for value in re.findall(r'=\s*"([^"]*)"', text):
        assert "<" not in value
        assert "alert" not in value


# -- privacy -----------------------------------------------------------------

USER_VALUE = "user-value-0000"


def _user_config(body: str) -> None:
    path = Path.home() / ".config" / "agentic-preflight" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _local_strings(repo: Path, store: Store, run_id: str) -> list[str]:
    run = store.load_run(run_id)
    values = [str(repo), str(repo.resolve()), str(store.root), str(Path.home())]
    if run.worktree_path:
        values.append(run.worktree_path)
    return values


def test_the_default_report_withholds_local_paths_and_user_config(feature_repo, tmp_path):
    configure_base(feature_repo, "[docs]\nenabled = false\n")
    _user_config(f"[commands]\nlint = 'true'\ntest = 'SAMPLE_TOKEN={USER_VALUE} true'\n")
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    store = Store(feature_repo / ".git" / "agentic-preflight")
    # A longer string that quotes the user-level command, as an error or a reason can.
    store.append_event(
        run_id,
        {"event": "noted", "reason": f"ran `SAMPLE_TOKEN={USER_VALUE} true` and it failed"},
    )

    shared = Path(agent.run("audit")["data"]["path"]).read_text(encoding="utf-8")

    assert USER_VALUE not in shared
    assert REDACTED_COMMAND in shared
    assert "user-level configuration sections: [commands]" in shared
    for value in _local_strings(feature_repo, store, run_id):
        assert value not in shared, value
        assert json.dumps(value)[1:-1] not in shared, value
    assert "&lt;validation worktree&gt;" in shared or "&lt;run store&gt;" in shared

    local = tmp_path / "local.html"
    env = agent.run("audit", "--local-details", "--output", str(local))
    assert env["data"]["local_details"] is True
    detailed = local.read_text(encoding="utf-8")
    assert USER_VALUE in detailed
    assert str(store.root) in detailed or str(store.root.resolve()) in detailed
    assert "Local details included." in detailed


def _remote_keys(data: dict) -> list[str]:
    """Every key that names a Git remote in the report's raw events."""
    found: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"remote", "sync_remote", "remote_url"}:
                    found.append(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for row in data["timeline"]["events"]:
        walk(row["raw"])
    return found


def test_the_shareable_report_drops_remote_names_from_raw_events(sample):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)

    # A remote can be a URL with credentials in it. The run section already withholds
    # it; the raw events must not bring it back.
    assert _remote_keys(build_audit(session, run, local_details=True)) != []
    shared = build_audit(session, run)
    assert _remote_keys(shared) == []
    assert shared["run"]["remote"] is None

    # A remote-tracking ref names the remote too; the shareable view masks the name.
    detailed = json.dumps(build_audit(session, run, local_details=True))
    assert "refs/remotes/origin/" in detailed
    assert "refs/remotes/origin/" not in json.dumps(shared)
    assert "refs/remotes/<remote>/" in json.dumps(shared)
    assert "refs/remotes/origin/" not in render(shared)


def test_a_run_without_recorded_config_provenance_withholds_its_snapshot(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    path = store.run_path(run_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["user_config_sections"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    session = open_session(feature_repo, run_id=run_id)
    data = build_audit(session, session.store.load_run(run_id))

    assert data["run"]["config_snapshot"] == WITHHELD_CONFIG
    created = next(e for e in data["timeline"]["events"] if e["event"] == "run_created")
    assert created["raw"]["config_snapshot"] == WITHHELD_CONFIG


# -- any state, any record ---------------------------------------------------


def test_audit_without_any_run_of_this_worktree_names_the_run_option(agent):
    env = agent.run("audit", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "no_run"
    assert "--run" in env["error"]["message"]


def test_a_fresh_run_with_no_findings_or_note_renders(agent):
    agent.run("start")
    env = agent.run("audit")
    assert env["data"]["run_selection"] == "active"
    text = Path(env["data"]["path"]).read_text(encoding="utf-8")
    assert env["data"]["attested"] is False
    assert env["data"]["note_verified"] is None
    assert "No findings were recorded." in text
    assert "no attestation note" in text
    assert "Not reached" in text
    # The run is still going, so its state is not final.
    assert "Current state" in text
    assert "Final state" not in text


def test_a_finished_run_shows_its_state_as_final(sample):
    agent, _repo, run_id = sample
    text = Path(agent.run("--run", run_id, "audit")["data"]["path"]).read_text(encoding="utf-8")
    assert "Final state" in text
    assert "Current state" not in text


def test_a_broken_event_log_is_reported_prominently(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    path = feature_repo / ".git" / "agentic-preflight" / "runs" / run_id / "events.jsonl"
    lines = path.read_bytes().split(b"\n")
    lines[0] = lines[0].replace(b"state_transition", b"state_transitioN")
    path.write_bytes(b"\n".join(lines))

    env = agent.run("audit")

    assert env["data"]["chain_status"] == "broken"
    text = Path(env["data"]["path"]).read_text(encoding="utf-8")
    assert "Event log broken" in text
    assert "seal--bad" in text
    assert "Line 2: prev_mismatch" in text


def test_a_missing_event_log_and_unreadable_findings_still_render(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    run_dir = feature_repo / ".git" / "agentic-preflight" / "runs" / run_id
    (run_dir / "events.jsonl").unlink()
    (run_dir / "findings.json").write_text("{not json", encoding="utf-8")

    env = agent.run("audit")

    assert env["data"]["chain_status"] == "empty"
    text = Path(env["data"]["path"]).read_text(encoding="utf-8")
    assert "No event log" in text
    assert "No events were recorded." in text
    assert "findings.json could not be read" in text


def test_a_log_from_an_earlier_release_renders_as_unchained(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    path = feature_repo / ".git" / "agentic-preflight" / "runs" / run_id / "events.jsonl"
    legacy = []
    for line in path.read_bytes().split(b"\n"):
        if line.strip():
            event = json.loads(line)
            event.pop("prev_sha256", None)
            event.pop("producer", None)
            legacy.append(json.dumps(event).encode())
    path.write_bytes(b"\n".join(legacy) + b"\n")
    record = path.parent / "run.json"
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload.pop("producer", None)
    record.write_text(json.dumps(payload), encoding="utf-8")

    env = agent.run("audit")

    assert env["data"]["chain_status"] == "unchained"
    text = Path(env["data"]["path"]).read_text(encoding="utf-8")
    assert "Event log not chained" in text
    assert "predates producer identity" in text
    assert "No reviewer report recorded." in text


# -- consistency, robustness, and refusals ------------------------------------


@pytest.mark.parametrize("command", ["ok", "log"])
def test_a_user_command_that_spells_a_display_value_does_not_break_the_report(
    feature_repo, tmp_path, command
):
    # "ok" is also the banner's tone, and "log" is in the banner's own wording.
    # Redaction must not rewrite what the renderer selects glyphs and styles by,
    # or the tool's own text.
    configure_base(feature_repo, "[docs]\nenabled = false\n")
    _user_config(f"[commands]\nlint = '{command}'\ntest = 'true'\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    text = Path(agent.run("audit")["data"]["path"]).read_text(encoding="utf-8")

    assert "Event log links intact" in text
    assert 'class="seal seal--ok"' in text
    assert "What this record proves" in text
    assert "does not show that the log is complete" in text


def test_a_short_user_command_does_not_rewrite_ordinary_words(feature_repo, tmp_path):
    configure_base(feature_repo, "[docs]\nenabled = false\n")
    _user_config("[commands]\nlint = 'make'\ntest = 'true'\n")
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start", "--intent", "make the cache thread-safe and construe nothing")[
        "run_id"
    ]
    unit = agent.run("context")["data"]["review_coverage"]["units"][0]
    finding = {
        "unit": unit["id"],
        "path": unit["path"],
        "severity": "low",
        "action": "no_op",
        "title": "make the cache thread-safe",
        "detail": "The remake of the lock is true to the design.",
    }
    agent.run("submit-findings", "--file", findings_json(tmp_path, [finding]))
    store = Store(feature_repo / ".git" / "agentic-preflight")
    store.append_event(
        run_id,
        {
            "event": "noted",
            "reason": "ran `make` and the remake failed",
            # A key that holds written text in a finding holds an error here.
            "detail": "command make exited 2",
        },
    )

    session = open_session(feature_repo, run_id=run_id)
    data = build_audit(session, session.store.load_run(run_id))

    # What a person wrote is kept as written: the intent and the finding's text.
    intent = "make the cache thread-safe and construe nothing"
    assert data["run"]["intent"] == intent
    (row,) = data["findings"]["items"]
    assert row["title"] == "make the cache thread-safe"
    assert row["detail"] == "The remake of the lock is true to the design."
    raws = [e["raw"] for e in data["timeline"]["events"]]
    # The same text in an event reads the same as it does in the run's own section.
    assert [raw["intent"] for raw in raws if "intent" in raw] == [intent]
    # Elsewhere a command is replaced as a whole token, never inside another word,
    # and a written-text key does not exempt a value that nobody wrote.
    noted = next(raw for raw in raws if raw.get("event") == "noted")
    assert noted["reason"] == f"ran `{REDACTED_COMMAND}` and the remake failed"
    assert noted["detail"] == f"command {REDACTED_COMMAND} exited 2"


@pytest.mark.parametrize(
    ("command", "text", "expected"),
    [
        ("make", "remake, make-do, make.", f"remake, {REDACTED_COMMAND}-do, {REDACTED_COMMAND}."),
        ("./check --", "run ./check --now", f"run {REDACTED_COMMAND}now"),
        ("-x run", "bash-x run, bash-x runs", f"bash{REDACTED_COMMAND}, bash-x runs"),
    ],
)
def test_a_command_is_replaced_as_a_whole_token(command, text, expected):
    # A boundary is required only at an end of the command that is a word character.
    assert audit_data._whole_token(command).sub(REDACTED_COMMAND, text) == expected


WITHHELD = audit_data.WITHHELD_PATH


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("python at /opt/tools/bin/python3 failed", f"python at {WITHHELD} failed"),
        ("wrote /private/var/tmp/x.log.", f"wrote {WITHHELD}."),
        ("--config=/etc/tool/conf.toml:12", f"--config={WITHHELD}:12"),
        ("see file:///opt/x/y", f"see file://{WITHHELD}"),
        (r"at C:\Tools\bin\lint.exe now", f"at {WITHHELD} now"),
        (r"share \\host\build\out.txt", f"share {WITHHELD}"),
        # Not a local path: a marker's tail, a relative path, a URL, a ref, a ratio.
        ("<repository>/src/app.py", "<repository>/src/app.py"),
        ("~/.config/tool and ./run.sh and ../up", "~/.config/tool and ./run.sh and ../up"),
        ("https://example.com/a/b and git@host:o/r.git", None),
        ("refs/remotes/<remote>/main and origin/main", None),
        ("3/4 done, and/or, a / b, </p>", None),
    ],
)
def test_a_path_outside_the_known_roots_is_withheld_by_shape(text, expected):
    assert audit_data._without_other_paths(text) == (text if expected is None else expected)


def test_a_path_outside_the_known_roots_is_withheld_from_event_text(feature_repo, tmp_path):
    configure_base(
        feature_repo,
        "[docs]\nenabled = false\n\n[commands]\nlint = '/usr/bin/env true'\ntest = 'true'\n",
    )
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start", "--intent", "serve /api/v1/orders from the cache")["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    store.append_event(
        run_id,
        {
            "event": "noted",
            "reason": "interpreter /opt/tools/bin/python3 is missing",
            "command": "/opt/tools/bin/python3 -m lint",
            "tool_path": "/opt/My Tools/bin/lint",
        },
    )

    session = open_session(feature_repo, run_id=run_id)
    data = build_audit(session, session.store.load_run(run_id))
    noted = next(e["raw"] for e in data["timeline"]["events"] if e["raw"].get("event") == "noted")

    assert "/opt/" not in json.dumps(data)
    assert noted["reason"] == f"interpreter {WITHHELD} is missing"
    # A command nobody published is free text; a value under a path key goes whole.
    assert noted["command"] == f"{WITHHELD} -m lint"
    assert noted["tool_path"] == WITHHELD
    # What the note publishes as written is kept: the intent, and the repository's
    # own command, in the configuration and wherever it is the stage's command.
    assert data["run"]["intent"] == "serve /api/v1/orders from the cache"
    assert data["run"]["config_snapshot"]["commands"]["lint"] == "/usr/bin/env true"
    assert WITHHELD in render(data)

    local = build_audit(session, session.store.load_run(run_id), local_details=True)
    assert "/opt/tools/bin/python3" in json.dumps(local)


def test_a_fixed_word_is_kept_only_where_it_is_one(feature_repo, tmp_path):
    configure_base(feature_repo, "[docs]\nenabled = false\n")
    _user_config(f"[commands]\nlint = 'SAMPLE_TOKEN={USER_VALUE} run'\ntest = 'true'\n")
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    # The log is untrusted: these keys usually hold a word from a fixed set, not here.
    store.append_event(
        run_id,
        {
            "event": "noted",
            "name": str(feature_repo / "private.txt"),
            "state": f"SAMPLE_TOKEN={USER_VALUE} run",
            "kind": {"action": f"SAMPLE_TOKEN={USER_VALUE} run"},
            "status": "green",
        },
    )

    session = open_session(feature_repo, run_id=run_id)
    data = build_audit(session, session.store.load_run(run_id))
    noted = next(e["raw"] for e in data["timeline"]["events"] if e["event"] == "noted")

    assert USER_VALUE not in json.dumps(data)
    assert str(feature_repo) not in json.dumps(noted)
    assert noted["state"] == REDACTED_COMMAND
    assert noted["kind"] == {"action": REDACTED_COMMAND}
    assert noted["status"] == "green"


def test_a_user_level_setup_command_is_withheld_from_events(feature_repo, tmp_path):
    configure_base(feature_repo, "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\n")
    setup = f"SAMPLE_TOKEN={USER_VALUE} ./prepare.sh"
    _user_config(f"[worktree]\nsetup_command = '{setup}'\n")
    agent = ScriptedAgent(feature_repo)
    # The setup command does not exist, so `start` fails and records it in the log.
    failed = agent.run("start", expect=ExitCode.STAGE_FAILED)
    assert failed["error"]["code"] == "setup_failed"
    store = Store(feature_repo / ".git" / "agentic-preflight")
    assert USER_VALUE in store.events_path(failed["run_id"]).read_text(encoding="utf-8")

    shared = Path(agent.run("audit")["data"]["path"]).read_text(encoding="utf-8")
    assert USER_VALUE not in shared
    assert REDACTED_COMMAND in shared

    local = tmp_path / "local.html"
    agent.run("audit", "--local-details", "--output", str(local))
    assert USER_VALUE in local.read_text(encoding="utf-8")


def test_the_note_that_is_shown_is_the_note_that_is_verified(sample):
    _agent, repo, _run_id = sample
    head = git("rev-parse", "HEAD", cwd=repo)
    value = attestation.read(repo, head)
    assert value is not None
    assert audit_data._verification(repo, head, value)["ok"] is True

    # The notes ref still holds the valid note. The verdict follows the note it is given.
    other = value.model_copy(update={"tree_sha": "0" * 40})
    verdict = audit_data._verification(repo, head, other)
    assert verdict["ok"] is False
    assert verdict["reason"] == "tree_mismatch"


@pytest.mark.parametrize("payload", ["null", "{}", "7"])
def test_a_findings_file_that_is_not_a_list_is_reported_not_fatal(agent, feature_repo, payload):
    run_id = agent.run("start")["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    store.findings_path(run_id).write_text(payload, encoding="utf-8")

    text = Path(agent.run("audit")["data"]["path"]).read_text(encoding="utf-8")
    assert "findings.json could not be read" in text


def test_an_empty_findings_list_renders_without_an_error(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    store.findings_path(run_id).write_text("[]", encoding="utf-8")

    text = Path(agent.run("audit")["data"]["path"]).read_text(encoding="utf-8")
    assert "findings.json could not be read" not in text
    assert "No findings were recorded." in text


def test_a_damaged_reviewer_source_does_not_stop_the_report(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    store.append_event(
        run_id,
        {
            "event": "reviewer_reported",
            "source": ["not", "a", "label"],
            "provider": "claude",
            "model": "m",
            "effort": "low",
            "prompts": [],
        },
    )

    text = Path(agent.run("audit")["data"]["path"]).read_text(encoding="utf-8")
    assert "Self-reported" in text
    assert "[&#x27;not&#x27;, &#x27;a&#x27;, &#x27;label&#x27;]" in text


def test_a_command_time_from_an_earlier_attempt_is_not_shown(sample):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    record = session.store.load_run(run_id).stages[Stage.LINT]
    assert record.started_at is not None

    after = (12.0, "2999-01-01T00:00:00+00:00")
    before = (12.0, "2000-01-01T00:00:00+00:00")
    assert audit_data._attempt_seconds(record, after) == 12.0
    # The timed run finished before this attempt began, so it belongs to another attempt.
    assert audit_data._attempt_seconds(record, before) is None
    assert audit_data._attempt_seconds(record, None) is None


def test_a_pushed_commit_without_a_push_event_is_still_shown(sample):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    data = build_audit(session, session.store.load_run(run_id))
    assert data["publication"]["pushed_sha"]

    data["publication"]["pushes"] = []
    assert "from the run record" in render(data)
    data["publication"]["pushed_sha"] = None
    assert "No push was recorded." in render(data)


def test_the_timeline_says_its_entries_are_reformatted_and_redacted(sample):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)

    shared = render(build_audit(session, run))
    assert "exactly as stored" not in shared
    assert "reformatted as JSON" in shared
    assert "This shareable view removes" in shared
    assert "Read it before you share it" in shared
    detailed = render(build_audit(session, run, local_details=True))
    assert "reformatted as JSON" in detailed
    assert "This shareable view removes" not in detailed


def test_output_inside_the_git_directory_is_refused(sample, tmp_path):
    agent, repo, run_id = sample
    store = Store(repo / ".git" / "agentic-preflight")
    record = store.run_path(run_id)
    before = record.read_bytes()

    for target in (record, repo / ".git" / "HEAD", store.run_dir(run_id) / "other.html"):
        env = agent.run(
            "--run", run_id, "audit", "--output", str(target), expect=ExitCode.PRECONDITION
        )
        assert env["error"]["code"] == "output_refused"
    assert record.read_bytes() == before
    assert json.loads(before)["run_id"] == run_id

    # A directory is not a file to write, and the run's own audit.html still works.
    env = agent.run(
        "--run", run_id, "audit", "--output", str(tmp_path), expect=ExitCode.PRECONDITION
    )
    assert env["error"]["code"] == "output_refused"
    default = store.run_dir(run_id) / "audit.html"
    explicit = agent.run("--run", run_id, "audit", "--output", str(default))
    assert explicit["data"]["path"] == str(default)
    assert agent.run("--run", run_id, "audit")["data"]["path"] == str(default)


def test_a_run_that_moves_during_the_capture_is_read_again(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)
    real = audit_data._findings
    moves = [1]

    def moving(session, run_id):
        # A run command lands between the reads, once.
        if moves:
            moves.pop()
            with session.store.transaction(run_id) as doc:
                doc.stale = doc.stale
        return real(session, run_id)

    monkeypatch.setattr(audit_data, "_findings", moving)
    data = build_audit(session, run)

    assert data["capture"] == {"consistent": True, "reason": None, "attempts": 2}
    assert "The run changed while this report was made" not in render(data)


def test_the_envelope_describes_the_run_the_report_shows(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    real = audit_data._findings
    moves = [1]

    def moving(session, run_id):
        # A command changes the run's state between the reads, once.
        if moves:
            moves.pop()
            with session.store.transaction(run_id) as doc:
                doc.state = State.ORPHANED
                doc.orphaned_reason = "superseded"
        return real(session, run_id)

    monkeypatch.setattr(audit_data, "_findings", moving)
    envelope = run_audit(session)

    assert isinstance(envelope.data, AuditData)
    text = Path(envelope.data.path).read_text(encoding="utf-8")
    assert envelope.state == "ORPHANED"
    assert "ORPHANED" in text


def test_the_shareable_view_lists_remotes_among_what_it_leaves_out(sample):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)

    shared = build_audit(session, run)
    local = build_audit(session, run, local_details=True)

    assert any("remote" in item for item in shared["withheld"])
    assert "a short ref such as origin/main is kept" in render(shared)
    assert local["withheld"] == []


def test_a_run_that_never_settles_is_reported_as_such(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)
    real = audit_data._findings

    def always_moving(session, run_id):
        with session.store.transaction(run_id) as doc:
            doc.stale = doc.stale
        return real(session, run_id)

    monkeypatch.setattr(audit_data, "_findings", always_moving)
    data = build_audit(session, run)

    assert data["capture"]["consistent"] is False
    assert data["capture"]["reason"] == "run_moved"
    assert "The run changed while this report was made" in render(data)


def test_a_run_record_that_cannot_be_read_again_is_not_called_consistent(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)

    def unreadable(_run_id):
        raise StoreError("run.json could not be read")

    monkeypatch.setattr(session.store, "load_run", unreadable)
    data = build_audit(session, run)

    # The record in hand is not compared with itself: the check did not happen.
    assert data["capture"] == {"consistent": False, "reason": "not_checked", "attempts": 3}
    text = render(data)
    assert "could not check that its records describe one moment" in text
    assert "The run changed while this report was made" not in text


def test_a_lock_that_cannot_be_probed_is_not_called_consistent(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)

    def unreadable(_run_id):
        raise OSError("operation lock could not be opened")

    monkeypatch.setattr(session.store, "try_operation", unreadable)
    data = build_audit(session, run)

    assert data["capture"] == {"consistent": False, "reason": "not_checked", "attempts": 3}
    assert "could not check that its records describe one moment" in render(data)


def test_the_capture_does_not_recreate_a_pruned_run(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)
    run_dir = session.store.run_dir(run_id)
    real = audit_data._findings

    def pruned(session, run_id):
        # gc prunes the run between the reads, lock file and directory included.
        found = real(session, run_id)
        shutil.rmtree(run_dir, ignore_errors=True)
        return found

    monkeypatch.setattr(audit_data, "_findings", pruned)
    data = build_audit(session, run)

    assert data["capture"]["reason"] == "not_checked"
    assert not run_dir.exists()


def test_a_command_that_holds_the_run_is_reported(sample):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)

    # A run command holds the operation lock for as long as it runs.
    with session.store.operation(run_id):
        data = build_audit(session, run)

    assert data["capture"] == {"consistent": False, "reason": "command_running", "attempts": 3}
    assert "A command was running on this run while this report was made" in render(data)
    assert build_audit(session, run)["capture"]["consistent"] is True


def test_a_report_made_between_a_response_and_its_event_says_so(tmp_path, monkeypatch):
    repo = _sample_repo(tmp_path)
    agent = ScriptedAgent(repo)
    run_id = agent.run("start", "--intent", INTENT)["run_id"]
    assert agent.run("review", "run")["state"] == "REVIEW_BLOCKED"
    session = open_session(repo)
    real = Store.append_event
    seen: dict = {}

    def paused(self, event_run_id, event):
        # respond has committed the finding and the run record, and is about to
        # append the event that says when. A report is made at this boundary.
        if event.get("event") == "finding_resolved":
            other = open_session(repo, run_id=run_id)
            seen["data"] = build_audit(other, other.store.load_run(run_id))
        return real(self, event_run_id, event)

    # The patch is scoped to the block: ``monkeypatch.undo()`` would also drop the
    # Git identity and the isolated home that the suite's fixtures set.
    # The CLI runs respond under the run's operation lock.
    with monkeypatch.context() as patch, session.store.operation(run_id):
        patch.setattr(Store, "append_event", paused)
        respond(
            session,
            finding_id="F002",
            action="dismissed",
            commit=None,
            note="Prices are stored in integer cents elsewhere.",
            reason="out_of_scope",
        )

    between = seen["data"]
    finding = next(item for item in between["findings"]["items"] if item["id"] == "F002")
    assert finding["status"] == "dismissed"
    assert finding["resolved_at"] is None
    assert between["capture"]["reason"] == "command_running"

    after = build_audit(session, session.store.load_run(run_id))
    finding = next(item for item in after["findings"]["items"] if item["id"] == "F002")
    assert finding["resolved_at"] is not None
    assert after["capture"]["consistent"] is True


def test_the_timeline_and_the_chain_come_from_one_read_of_the_log(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)
    real = session.store.read_event_bytes
    reads = []

    def counted(event_run_id):
        reads.append(event_run_id)
        # A line is rewritten in place after the read, without changing the line count.
        content = real(event_run_id)
        path = session.store.events_path(event_run_id)
        path.write_bytes(content.replace(b"finished", b"finishEd"))
        return content

    monkeypatch.setattr(session.store, "read_event_bytes", counted)
    data = build_audit(session, run)

    assert reads == [run_id]
    assert data["chain"]["status"] == "intact"
    assert data["chain"]["lines"] == len(data["timeline"]["events"])
    assert [row["event"] for row in data["timeline"]["events"]][-1] == "finished"


def test_a_log_an_earlier_release_started_is_not_called_fully_chained(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    path = store.events_path(run_id)
    legacy = []
    for line in path.read_bytes().split(b"\n"):
        if line.strip():
            event = json.loads(line)
            event.pop("prev_sha256", None)
            legacy.append(json.dumps(event).encode())
    # An interrupted write leaves a line that is not an event. The next append chains to it.
    path.write_bytes(b"\n".join(legacy) + b'\n{"event": "tor')
    store.append_event(run_id, {"event": "noted"})

    session = open_session(feature_repo, run_id=run_id)
    data = build_audit(session, session.store.load_run(run_id))
    chain = data["chain"]

    assert chain["status"] == "intact"
    assert (chain["unchained_lines"], chain["torn_lines"]) == (len(legacy), 1)
    assert chain["banner"]["tone"] == "warn"
    assert chain["banner"]["headline"] == "Event log partly chained"
    assert f"{len(legacy)} line" in chain["banner"]["detail"]
    assert "with no link before its first chained line" in chain["banner"]["detail"]
    assert "1 line that could not be read as an event" in chain["banner"]["detail"]
    assert "Event log partly chained" in render(data)


def test_a_torn_line_alone_does_not_make_a_chained_log_partly_chained(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    path = store.events_path(run_id)
    # An interrupted write leaves a line that is not an event. The next append chains to it.
    path.write_bytes(path.read_bytes().rstrip(b"\n") + b'\n{"event": "tor')
    store.append_event(run_id, {"event": "noted"})

    session = open_session(feature_repo, run_id=run_id)
    chain = build_audit(session, session.store.load_run(run_id))["chain"]

    assert chain["status"] == "intact"
    assert (chain["unchained_lines"], chain["torn_lines"]) == (0, 1)
    assert chain["banner"]["tone"] == "ok"
    assert chain["banner"]["headline"] == "Event log links intact"
    assert "1 line that could not be read as an event" in chain["banner"]["detail"]
    assert "with no link" not in chain["banner"]["detail"]


def test_a_fully_chained_log_claims_only_the_links_that_were_checked(sample):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    banner = build_audit(session, session.store.load_run(run_id))["chain"]["banner"]

    # The finished sample run has a note, so its log is also checked against the anchor.
    assert banner["headline"] == "Event log links intact and anchored"
    assert banner["detail"].startswith("Each chained line of the event log")
    assert "Every line" not in banner["detail"]
    assert "with no link" not in banner["detail"]
    assert "does not show that the log is complete" in banner["detail"]


def test_a_time_that_is_not_a_number_of_seconds_does_not_stop_the_report(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    # JSON can hold an integer too large for a float, and Python's JSON a non-finite one.
    for command, seconds in (("lint", 10**400), ("test", float("inf")), ("review", -1)):
        store.append_event(
            run_id, {"event": "tool_command_finished", "command": command, "seconds": seconds}
        )
    store.append_event(run_id, {"event": "tool_command_finished", "command": "docs", "seconds": 2})

    session = open_session(feature_repo, run_id=run_id)
    events = session.store.load_events(run_id)
    timed = audit_data._command_seconds(events)
    assert timed["docs"][0] == 2.0
    assert not {"lint", "test", "review"} & set(timed)
    assert agent.run("audit")["ok"] is True
    # The renderer takes plain data, which can come from a file, so it checks too.
    for value in (float("nan"), float("inf"), -1, True, 10**400):
        assert "not recorded" in render_duration(value)
    assert render_duration(2) == "2 s"


def test_a_verification_diagnostic_is_redacted_like_other_text(sample, monkeypatch):
    _agent, repo, run_id = sample

    def refuse(*_args, **_kwargs):
        raise attestation.InvalidAttestation(
            "base /opt/ci/base-policy does not name a commit", reason="base_mismatch"
        )

    monkeypatch.setattr(attestation, "verify_value", refuse)
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)
    data = build_audit(session, run)

    verdict = data["publication"]["attestation"]["verification"]
    assert verdict["ok"] is False
    assert verdict["detail"] == f"base {WITHHELD} does not name a commit"
    assert "/opt/ci" not in render(data)
    # A commit subject is still kept as written.
    assert data["publication"]["fix_commits"][0]["subject"] == (
        "Reject a non-positive quantity in unit_price"
    )
    local = build_audit(session, run, local_details=True)
    assert "/opt/ci/base-policy" in local["publication"]["attestation"]["verification"]["detail"]


def test_a_gate_commit_subject_is_kept_as_written(sample, monkeypatch):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    run = session.store.load_run(run_id)
    real = audit_data._subject
    subject = "Serve prices from /srv/pricing/cache"

    def path_in_subject(subject_repo, sha):
        # A commit that only the gate lists has a subject shaped like a local path.
        return real(subject_repo, sha) if sha in run.fix_commits else subject

    monkeypatch.setattr(audit_data, "_subject", path_in_subject)
    data = build_audit(session, run)

    gated = [
        commit
        for gate in data["publication"]["gates"]
        for commit in gate["commits"]
        if commit["sha"] not in run.fix_commits
    ]
    assert gated
    assert all(commit["subject"] == subject for commit in gated)
    assert audit_data._without_other_paths(subject) != subject


def test_a_directory_at_the_default_report_path_is_refused(sample):
    agent, repo, run_id = sample
    store = Store(repo / ".git" / "agentic-preflight")
    default = store.run_dir(run_id) / "audit.html"
    default.mkdir()

    for arguments in (["audit"], ["audit", "--output", str(default)]):
        env = agent.run("--run", run_id, *arguments, expect=ExitCode.PRECONDITION)
        assert env["error"]["code"] == "output_refused"
    assert default.is_dir()


def test_a_worktree_git_pointer_file_is_refused(sample, tmp_path):
    agent, repo, run_id = sample
    linked = tmp_path / "linked"
    git("worktree", "add", "--detach", str(linked), cwd=repo)
    pointer = linked / ".git"
    before = pointer.read_bytes()
    assert before.startswith(b"gitdir: ")

    env = agent.run(
        "--run", run_id, "audit", "--output", str(pointer), expect=ExitCode.PRECONDITION
    )

    assert env["error"]["code"] == "output_refused"
    assert pointer.read_bytes() == before
    assert git("rev-parse", "--is-inside-work-tree", cwd=linked) == "true"


def _known_worktree(root: Path) -> tuple[SimpleNamespace, SimpleNamespace]:
    session = SimpleNamespace(caller_root=None, repo_root=None)
    run = SimpleNamespace(worktree_path=str(root), source_worktree_path=None)
    return session, run


def test_a_known_worktree_git_file_is_refused_by_path(tmp_path, monkeypatch):
    known = tmp_path / "known"
    other = tmp_path / "other"
    for root in (known, other):
        root.mkdir()
        # Not a pointer by its content, so only the path can identify it.
        (root / ".git").write_bytes(b"not a pointer\n")
    session, run = _known_worktree(known)

    assert run_audit_module._is_git_pointer(session, run, (known / ".git").resolve())
    assert not run_audit_module._is_git_pointer(session, run, (other / ".git").resolve())

    def unreadable(*_args, **_kwargs):
        raise PermissionError("cannot read")

    # A file that cannot be read is not called a pointer.
    (other / ".git").write_bytes(b"gitdir: /elsewhere\n")
    monkeypatch.setattr(run_audit_module, "open", unreadable, raising=False)
    assert not run_audit_module._is_git_pointer(session, run, (other / ".git").resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="named pipes need os.mkfifo")
@pytest.mark.timeout(60)
def test_output_to_a_named_pipe_is_not_opened_for_reading(sample, tmp_path):
    agent, _repo, run_id = sample
    pipe = tmp_path / "report.html"
    os.mkfifo(pipe)

    # Opening the pipe to read it would wait for a writer that never comes.
    env = agent.run("--run", run_id, "audit", "--output", str(pipe))

    assert env["data"]["path"] == str(pipe.resolve())
    assert pipe.is_file()


def test_render_accepts_the_data_alone(sample):
    _agent, repo, run_id = sample
    session = open_session(repo, run_id=run_id)
    data = json.loads(json.dumps(build_audit(session, session.store.load_run(run_id))))
    assert render(data).count("<section") == 7


# -- the checked-in example --------------------------------------------------


@pytest.mark.skipif(
    "AUDIT_REPORT_EXAMPLE" not in os.environ,
    reason="set AUDIT_REPORT_EXAMPLE to a path to regenerate docs/examples/audit-report.html",
)
def test_write_the_example_report(sample):
    agent, _repo, _run_id = sample
    out = Path(os.environ["AUDIT_REPORT_EXAMPLE"]).resolve()
    agent.run("audit", "--output", str(out))
