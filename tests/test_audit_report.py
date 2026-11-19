"""The ``audit`` command: one run's audit trail as a self-contained HTML report."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from agentic_preflight import attestation, gitx
from agentic_preflight.envelope import ExitCode
from agentic_preflight.public_config import REDACTED_COMMAND
from agentic_preflight.report.audit import WITHHELD_CONFIG, build_audit
from agentic_preflight.report.audit_html import render
from agentic_preflight.runs import open_session
from agentic_preflight.store import Store
from tests.conftest import commit_all, configure_base, git, write
from tests.driver import PROTECTED_CONFIG, ScriptedAgent, findings_json

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
    assert "Event log intact" in text


def test_audit_selects_a_finished_run_with_the_global_run_option(sample, tmp_path):
    agent, _repo, run_id = sample
    assert agent.run("audit")["data"]["run_selection"] == "most_recent"

    out = tmp_path / "out" / "report.html"
    env = agent.run("--run", run_id, "audit", "--output", str(out))

    assert env["run_id"] == run_id
    assert env["data"]["run_selection"] == "selected"
    assert env["data"]["path"] == str(out.resolve())
    assert out.is_file()


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
