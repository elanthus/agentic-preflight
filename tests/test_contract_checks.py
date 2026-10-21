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
from agentic_preflight.contract_models import ContractPlan, ContractReceipt
from agentic_preflight.contract_runner import Capture, execute_plan, export_snapshot
from agentic_preflight.digests import json_digest
from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent, findings_json

PIN = "sha256:" + "a" * 64


def plan_for(manifest="a" * 64, units=("U0001",)):
    return {
        "schema_version": "2",
        "manifest": manifest,
        "units": [
            {
                "unit": unit,
                "disposition": "checks",
                "checks": ["C001"],
                "reason": "The changed operation has a preservation contract.",
                "inventory": [
                    {
                        "transformation": "Count a constructed iterable",
                        "input_classes": [
                            {
                                "name": "Constructed finite lists",
                                "disposition": "probes",
                                "probes": [
                                    {"check": "C001", "kind": kind}
                                    for kind in ("ordinary", "boundary", "composition")
                                ],
                                "reason": "Compare the result with the constructed length.",
                            }
                        ],
                    }
                ],
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
                "oracle_provenance": {
                    "kind": "derived_property",
                    "source": "Constructed list fixture",
                    "rationale": "Expected cardinality comes from fixture construction.",
                    "input_domain": "Finite lists of known cardinality",
                    "runtime": "Python 3 builtin list semantics",
                    "limitations": ["Does not establish behavior for infinite iterables."],
                },
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
    "mutation",
    [
        "missing_inventory",
        "missing_provenance",
        "blank_source",
        "unmapped_probe",
        "foreign_check",
        "duplicate_probe",
        "duplicate_class",
        "duplicate_transformation",
        "gap_with_probe",
        "covered_without_probe",
        "skip_with_inventory",
    ],
)
def test_behavioral_claims_require_provenance_and_resolvable_probe_references(mutation):
    data = plan_for()
    unit, check = data["units"][0], data["checks"][0]
    behavior = unit["inventory"][0]
    item = behavior["input_classes"][0]
    if mutation == "missing_inventory":
        del unit["inventory"]
    elif mutation == "missing_provenance":
        del check["oracle_provenance"]
    elif mutation == "blank_source":
        check["oracle_provenance"]["source"] = "   "
    elif mutation == "unmapped_probe":
        item["probes"].pop()
    elif mutation == "foreign_check":
        item["probes"][0]["check"] = "C002"
    elif mutation == "duplicate_probe":
        item["probes"].append(copy.deepcopy(item["probes"][0]))
    elif mutation == "duplicate_class":
        behavior["input_classes"].append(copy.deepcopy(item))
    elif mutation == "duplicate_transformation":
        unit["inventory"].append(copy.deepcopy(behavior))
    elif mutation == "gap_with_probe":
        item["disposition"] = "unverified"
    elif mutation == "covered_without_probe":
        item["probes"] = []
    else:
        unit.update(disposition="not_applicable", checks=[])
        data["checks"] = []
    with pytest.raises(ValidationError):
        ContractPlan.model_validate(data)


def as_legacy_plan(data):
    data = copy.deepcopy(data)
    data["schema_version"] = "1"
    for unit in data["units"]:
        unit.pop("inventory", None)
    for check in data["checks"]:
        check.pop("oracle_provenance", None)
    return data


def test_new_plan_schema_refuses_legacy_claims():
    with pytest.raises(ValidationError):
        ContractPlan.model_validate(as_legacy_plan(plan_for()))


def test_unverified_input_class_is_visible_even_when_all_probes_pass(feature_repo, monkeypatch):
    monkeypatch.setattr(contract_runner, "resolve_on_path", lambda _: "/trusted/docker")
    monkeypatch.setattr(contract_runner, "_capture", lambda *a, **kw: Capture(0, b"", b""))
    data = plan_for()
    data["units"][0]["inventory"][0]["input_classes"].append(
        {
            "name": "External iterator semantics",
            "disposition": "unverified",
            "probes": [],
            "reason": "The external iterator contract is unavailable.",
        }
    )
    receipt = execute_plan(
        feature_repo,
        gitx.rev_parse(feature_repo, "HEAD"),
        ContractPlan.model_validate(data),
        ReviewContractsSection(enabled=True, image=PIN),
    )
    assert all(p.status == "passed" for c in receipt.checks for p in c.probes)
    assert receipt.status == "unverified"


def test_legacy_receipt_is_readable_without_rewriting_its_digest_bound_plan(
    feature_repo, monkeypatch
):
    monkeypatch.setattr(contract_runner, "resolve_on_path", lambda _: None)
    receipt = execute_plan(
        feature_repo,
        gitx.rev_parse(feature_repo, "HEAD"),
        ContractPlan.model_validate(plan_for()),
        ReviewContractsSection(),
    )
    wire = receipt.model_dump(mode="json")
    wire["schema_version"] = "1"
    wire["plan"] = as_legacy_plan(wire["plan"])
    wire["plan_sha256"] = json_digest(wire["plan"])
    restored = ContractReceipt.model_validate(wire)
    assert restored.model_dump(mode="json") == wire
    assert json_digest(restored.plan.model_dump(mode="json")) == wire["plan_sha256"]
    wire["schema_version"] = "2"
    with pytest.raises(ValidationError, match="schema versions must match"):
        ContractReceipt.model_validate(wire)


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


@pytest.mark.parametrize("large", [None, "code", "inventory"])
def test_final_handoff_binds_claims_assertions_and_explicit_omissions(
    feature_repo, tmp_path, monkeypatch, large
):
    monkeypatch.setattr(contract_runner, "resolve_on_path", lambda _: None)
    enable(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    def expand(plan):
        if large == "code":
            for probe in plan["checks"][0]["probes"]:
                probe["code"] = "#" + "x" * 10_000
        elif large == "inventory":
            for unit in plan["units"]:
                original = unit["inventory"][0]["input_classes"][0]
                unit["inventory"][0]["input_classes"] = [
                    {**original, "name": f"Class {index}", "reason": "r" * 1000}
                    for index in range(32)
                ]

    path = deliver_plan(agent, tmp_path, expand)
    summary = agent.run("review", "contracts", "--file", str(path))["data"]["contract_checks"]
    handoff = summary["receipt"]
    full = json.loads(Path(handoff["path"]).read_text())
    assert handoff["sha256"] == json_digest(full)
    assert handoff["checks"][0]["plan_pointer"] == "/plan/checks/0"
    assert handoff["handoff_complete"] is (large is None)
    assert handoff["probe_code_included"] is (large != "code")
    assert bool(handoff["inventory_units_omitted"]) is (large == "inventory")
    assert bool(handoff["details_omitted"]) is (large == "code")
    if large != "code":
        detail = handoff["details"][0]
        check = full["plan"]["checks"][0]
        for name in ("producer", "consumer", "oracle", "oracle_provenance"):
            assert detail[name] == check[name]
        assert detail["probes"][0]["code"] == check["probes"][0]["code"]
        assert detail["probes"][0]["status"] == "unverified"
    assert (
        len(json.dumps(handoff["details"]).encode())
        + len(json.dumps(handoff["inventory"]).encode())
        < 16_100
    )
    assert summary["execution"]["total_timeout_seconds"] == 60


def test_old_receipt_is_preserved_but_requires_a_new_plan(feature_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(contract_runner, "resolve_on_path", lambda _: None)
    enable(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    plan = deliver_plan(agent, tmp_path)
    checked = agent.run("review", "contracts", "--file", str(plan))
    path = Path(checked["data"]["contract_checks"]["receipt"]["path"])
    old = json.loads(path.read_text())
    old["schema_version"] = "1"
    old["plan"] = as_legacy_plan(old["plan"])
    old["plan_sha256"] = json_digest(old["plan"])
    digest = json_digest(old)
    old_path = path.with_name(digest + ".json")
    old_bytes = json.dumps(old).encode()
    old_path.write_bytes(old_bytes)
    path.with_name("current.json").write_text(json.dumps({"sha256": digest}))
    context = agent.run("context")["data"]["contract_checks"]
    assert context["receipt"] is None
    assert context["plan_schema"]["properties"]["schema_version"]["const"] == "2"
    assert context["previous_receipt"]["sha256"] == digest
    agent.run(
        "submit-findings", "--file", findings_json(tmp_path, []), expect=ExitCode.PRECONDITION
    )
    agent.run("review", "contracts", "--file", str(plan))
    assert old_path.read_bytes() == old_bytes


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
