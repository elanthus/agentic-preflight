"""Generic contract planning and bounded execution, without model providers."""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_preflight import contract_runner, gitx
from agentic_preflight.config import ReviewContractsSection
from agentic_preflight.contract_models import ContractPlan
from agentic_preflight.contract_runner import Capture, execute_plan, export_snapshot
from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent, findings_json

PIN = "sha256:" + "a" * 64


def plan_for(manifest="a" * 64, units=("U0001",)):
    return {
        "schema_version": "1",
        "manifest": manifest,
        "units": [
            {
                "unit": unit,
                "disposition": "checks",
                "checks": ["C001"],
                "reason": "The changed operation has a preservation contract.",
            }
            for unit in units
        ],
        "checks": [
            {
                "id": "C001",
                "contract": "Counting preserves list length.",
                "producer": "An iterable producer",
                "consumer": "A length consumer",
                "oracle": "The number of constructed elements, independent of implementation.",
                "probes": [
                    {"kind": kind, "code": "assert len([2, 3]) == 2"}
                    for kind in ("ordinary", "boundary", "composition")
                ],
            }
        ],
    }


def test_empty_review_manifest_can_be_accounted_for():
    data = plan_for(units=())
    data["checks"] = []
    assert ContractPlan.model_validate(data).units == []


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_unit", "duplicate_check", "missing_class", "unknown_check", "unexplained_skip"],
)
def test_plan_rejects_incomplete_or_ambiguous_accounting(mutation):
    data = plan_for()
    if mutation == "duplicate_unit":
        data["units"].append(copy.deepcopy(data["units"][0]))
    elif mutation == "duplicate_check":
        data["checks"].append(copy.deepcopy(data["checks"][0]))
    elif mutation == "missing_class":
        data["checks"][0]["probes"][2]["kind"] = "boundary"
    elif mutation == "unknown_check":
        data["units"][0]["checks"] = ["C002"]
    else:
        data["units"][0]["disposition"] = "not_applicable"
    with pytest.raises(ValidationError):
        ContractPlan.model_validate(data)


@pytest.mark.parametrize(
    "image", ["python:latest", "--privileged", "sha256:abc", "x@sha256:" + "G" * 64]
)
def test_runtime_requires_an_immutable_operator_selected_image(image):
    with pytest.raises(ValidationError):
        ReviewContractsSection(image=image)


def test_snapshot_uses_committed_bytes_and_excludes_host_inputs(feature_repo, tmp_path):
    write(feature_repo, "module.py", "ANSWER = 7\n")
    commit_all(feature_repo, "add contract snapshot fixture")
    head = gitx.rev_parse(feature_repo, "HEAD")
    write(feature_repo, "module.py", "ANSWER = 99\n")
    write(feature_repo, ".env", "PRIVATE=host-only\n")
    output = tmp_path / "snapshot"
    digest = export_snapshot(feature_repo, head, output, time.monotonic() + 10)
    assert (output / "module.py").read_text() == "ANSWER = 7\n"
    assert not (output / ".env").exists()
    assert not (output / ".git").exists()
    assert len(digest) == 64


def test_snapshot_rejects_symlinks_before_any_probe(feature_repo, tmp_path):
    if os.name == "nt":
        pytest.skip("symlink creation requires developer privileges on Windows")
    (feature_repo / "outside").symlink_to(tmp_path)
    commit_all(feature_repo, "add contract snapshot fixture")
    with pytest.raises(ValueError, match="symlinks"):
        export_snapshot(
            feature_repo,
            gitx.rev_parse(feature_repo, "HEAD"),
            tmp_path / "snapshot",
            time.monotonic() + 10,
        )


@pytest.mark.parametrize(
    "name", ["x/CON.py", "x/name:stream", "x/end.", "x/new\nline", "x/.GiT/config"]
)
def test_snapshot_refuses_nonportable_names(name):
    assert not contract_runner._portable_name(name)


@pytest.mark.parametrize("budget", ["files", "bytes"])
def test_snapshot_budgets_fail_closed(feature_repo, tmp_path, monkeypatch, budget):
    monkeypatch.setattr(
        contract_runner, "MAX_SNAPSHOT_FILES" if budget == "files" else "MAX_SNAPSHOT_BYTES", 0
    )
    with pytest.raises(ValueError, match="budget"):
        export_snapshot(
            feature_repo,
            gitx.rev_parse(feature_repo, "HEAD"),
            tmp_path / "snapshot",
            time.monotonic() + 10,
        )


def test_pipe_capture_bounds_both_streams_and_terminates(tmp_path):
    result = contract_runner._capture(
        [
            sys.executable,
            "-c",
            "import sys;sys.stderr.write('e'*100000);sys.stdout.write('o'*100000)",
        ],
        3,
        1024,
        cwd=tmp_path,
    )
    assert len(result.stdout) + len(result.stderr) <= 1024
    assert result.reason == "output limit exceeded"
    result = contract_runner._capture(
        [sys.executable, "-c", "import time;time.sleep(30)"], 0.05, 1024, cwd=tmp_path
    )
    assert result.reason == "execution timed out"


def test_unavailable_execution_is_never_called_passed(feature_repo, monkeypatch):
    monkeypatch.setattr(contract_runner, "resolve_on_path", lambda _: None)
    receipt = execute_plan(
        feature_repo,
        gitx.rev_parse(feature_repo, "HEAD"),
        ContractPlan.model_validate(plan_for()),
        ReviewContractsSection(enabled=True),
    )
    assert receipt.status == "unverified"
    assert all(p.status == "unverified" for c in receipt.checks for p in c.probes)


@pytest.mark.parametrize(
    ("exit_code", "status"),
    [(0, "passed"), (1, "failed"), (124, "unverified"), (125, "unverified"), (137, "unverified")],
)
def test_probe_statuses_and_restrictions(feature_repo, monkeypatch, exit_code, status):
    calls = []

    def capture(argv, timeout, limit, *, cwd):
        calls.append(argv)
        return Capture(exit_code if argv[1] == "run" else 0, b"output", b"")

    monkeypatch.setattr(contract_runner, "resolve_on_path", lambda _: "/trusted/docker")
    monkeypatch.setattr(contract_runner, "_capture", capture)
    receipt = execute_plan(
        feature_repo,
        gitx.rev_parse(feature_repo, "HEAD"),
        ContractPlan.model_validate(plan_for()),
        ReviewContractsSection(enabled=True, image=PIN),
    )
    assert receipt.status == status
    assert len(calls) == 6  # Three probes, each followed by forced cleanup.
    for argv in calls[::2]:
        assert "--network=none" in argv
        assert "--pull=never" in argv
        assert "--read-only" in argv
        assert "--pids-limit=64" in argv
        assert "--memory=256m" in argv
        assert "--cpus=1" in argv
        assert "--security-opt=no-new-privileges" in argv
        assert all("readonly" in argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount")


def enable(repo, extra=""):
    write(
        repo,
        ".agentic-preflight.toml",
        '[worktree]\nmode="in_place"\ncopy_files=[]\n[docs]\nenabled=false\n'
        "[review.contracts]\nenabled=true\n" + extra,
    )
    commit_all(repo, "enable contract checks")


def deliver_plan(agent, tmp_path, transform=None):
    data = agent.run("context")["data"]
    plan = plan_for(
        data["review_coverage"]["manifest"], [u["id"] for u in data["review_coverage"]["units"]]
    )
    if transform:
        transform(plan)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return path


def test_in_harness_plan_is_required_and_receipt_is_bound(feature_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(contract_runner, "resolve_on_path", lambda _: None)
    enable(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    context = agent.run("context")
    assert context["next"]["command"].startswith("agentic-preflight review contracts")
    agent.run(
        "submit-findings", "--file", findings_json(tmp_path, []), expect=ExitCode.PRECONDITION
    )
    path = deliver_plan(agent, tmp_path)
    checked = agent.run("review", "contracts", "--file", str(path))
    assert checked["data"]["contract_checks"]["receipt"]["status"] == "unverified"
    result = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert len(result["data"]["coverage"]["contracts_sha256"]) == 64


@pytest.mark.parametrize("mutation", ["missing_unit", "stale_manifest", "too_many_checks"])
def test_invalid_plans_cannot_create_receipts(feature_repo, tmp_path, mutation):
    enable(feature_repo, "max_checks=1\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    def change(plan):
        if mutation == "missing_unit":
            plan["units"] = []
        elif mutation == "stale_manifest":
            plan["manifest"] = "0" * 64
        else:
            second = copy.deepcopy(plan["checks"][0])
            second["id"] = "C002"
            plan["checks"].append(second)
            plan["units"][0]["checks"].append("C002")

    path = deliver_plan(agent, tmp_path, change)
    agent.run("review", "contracts", "--file", str(path), expect=ExitCode.PRECONDITION)


def test_tampered_receipt_is_refused(feature_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(contract_runner, "resolve_on_path", lambda _: None)
    enable(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    path = deliver_plan(agent, tmp_path)
    env = agent.run("review", "contracts", "--file", str(path))
    receipt = Path(env["data"]["contract_checks"]["receipt"]["path"])
    data = json.loads(receipt.read_text())
    data["status"] = "passed"
    receipt.write_text(json.dumps(data))
    agent.run(
        "submit-findings", "--file", findings_json(tmp_path, []), expect=ExitCode.PRECONDITION
    )


@pytest.mark.parametrize("mode", ["valid", "invalid", "nonzero", "capture"])
def test_command_review_plans_before_final_findings(feature_repo, tmp_path, monkeypatch, mode):
    monkeypatch.setattr(contract_runner, "resolve_on_path", lambda _: None)
    fixture = plan_for()
    script = tmp_path / "reviewer.py"
    calls = tmp_path / "calls.jsonl"
    script.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "data=json.load(sys.stdin)\n"
        f"with Path({str(calls)!r}).open('a') as f:f.write(json.dumps(data)+'\\n')\n"
        "if data['section']=='contracts':\n"
        f" if {mode!r}=='nonzero':raise SystemExit(7)\n"
        f" if {mode!r}=='invalid':print('not JSON');raise SystemExit(0)\n"
        f" plan={fixture!r}\n"
        " plan['manifest']=data['review_coverage']['manifest']\n"
        " plan['units']=[{**plan['units'][0],'unit':u['id']} for u in data['review_coverage']['units']]\n"
        " print(json.dumps(plan))\n"
        "else:\n"
        " assert data['contract_checks']['receipt']['status']=='unverified'\n"
        " print(json.dumps({'coverage':{'manifest':data['review_coverage']['manifest'],'examined':'all'},'findings':[]}))\n"
    )
    command = shlex.join([sys.executable, str(script)])
    write(
        feature_repo,
        ".agentic-preflight.toml",
        '[worktree]\nmode="in_place"\ncopy_files=[".env"]\n'
        '[docs]\nenabled=false\n[review]\nexecutor="command"\ncommand='
        + json.dumps(command)
        + "\n[review.contracts]\nenabled=true\nplanner_command="
        + json.dumps(command)
        + "\n",
    )
    commit_all(feature_repo, "configure offline fake contract planner")
    if mode == "capture":
        (feature_repo / ".env").write_bytes(b"SECRET=\xff\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    if mode == "capture":
        for _ in range(2):
            env = agent.run("review", "run", expect=ExitCode.STAGE_FAILED)
            assert env["state"] == "REVIEW_AWAITING_FINDINGS"
            assert env["data"]["phase"] == "contracts"
            assert env["data"]["reviewer_invocations"] == 0
            assert "review" not in agent.run("status")["data"]["stages"]
            assert not calls.exists()
        (feature_repo / ".env").write_text("SECRET=readable-again\n")
    env = agent.run(
        "review", "run", expect=0 if mode in {"valid", "capture"} else ExitCode.STAGE_FAILED
    )
    delivered = [json.loads(line) for line in calls.read_text().splitlines()]
    if mode in {"valid", "capture"}:
        assert [d["section"] for d in delivered] == ["contracts", "review"]
        assert env["data"]["reviewer_invocations"] == 2
        assert env["data"]["contract_checks"]["receipt"]["status"] == "unverified"
        assert env["data"]["coverage"]["contracts_sha256"]
        assert agent.run("status")["data"]["stages"]["review"]["attempts"] == 0
    else:
        assert [d["section"] for d in delivered] == ["contracts"]
        assert env["state"] == "REVIEW_COMMAND_RED"
        assert agent.run("status")["data"]["stages"]["review"]["attempts"] == 1


@pytest.mark.skipif(
    not os.environ.get("AP_CONTRACT_TEST_IMAGE"), reason="explicit local cached-image integration"
)
@pytest.mark.parametrize("wall_clock_offset", [-3600, 3600])
def test_real_container_isolation_and_watchdog(feature_repo, monkeypatch, wall_clock_offset):
    config = ReviewContractsSection(
        enabled=True, image=os.environ["AP_CONTRACT_TEST_IMAGE"], timeout_seconds=2
    )
    data = plan_for()
    data["checks"][0]["probes"] = [
        {
            "kind": "ordinary",
            "code": "import os;assert os.getuid()==65534;assert os.getenv('AP_CONTRACT_HOST_SECRET') is None",
        },
        {
            "kind": "boundary",
            "code": "import os\ntry: os.kill(1,9)\nexcept PermissionError: pass\nelse: raise AssertionError('watchdog is killable')\ntry: open('/workspace/forbidden','w')\nexcept OSError: pass\nelse: raise AssertionError('source is writable')",
        },
        {
            "kind": "composition",
            "code": "import socket\ns=socket.socket();s.settimeout(.2)\ntry: s.connect(('1.1.1.1',53))\nexcept OSError: pass\nelse: raise AssertionError('network is available')",
        },
    ]
    monkeypatch.setenv("AP_CONTRACT_HOST_SECRET", "host-only-sentinel")
    original_time = time.time
    monkeypatch.setattr(contract_runner.time, "time", lambda: original_time() + wall_clock_offset)
    receipt = execute_plan(
        feature_repo,
        gitx.rev_parse(feature_repo, "HEAD"),
        ContractPlan.model_validate(data),
        config,
    )
    assert receipt.status == "passed", receipt.model_dump()
    for probe in data["checks"][0]["probes"]:
        probe["code"] = (
            "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True);time.sleep(60)"
        )
    # The watchdog must stop descendants itself regardless of host/engine clock
    # skew. Allow the attached client longer to observe that exit.
    original_capture = contract_runner._capture

    def observe_watchdog(argv, timeout, limit, *, cwd):
        return original_capture(argv, timeout + 3 if argv[1] == "run" else timeout, limit, cwd=cwd)

    monkeypatch.setattr(contract_runner, "_capture", observe_watchdog)
    receipt = execute_plan(
        feature_repo,
        gitx.rev_parse(feature_repo, "HEAD"),
        ContractPlan.model_validate(data),
        config,
    )
    assert receipt.status == "unverified"
    assert all(
        probe.exit_code == 124 and probe.reason == "runtime or resource failure"
        for check in receipt.checks
        for probe in check.probes
    )
    names = [probe.container_name for check in receipt.checks for probe in check.probes]
    containers = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not set(names).intersection(containers.stdout.splitlines())
