"""Declared shell inputs are captured without launching a stage."""

import sys

import pytest
from pydantic import ValidationError

from agentic_preflight.fingerprints import Disposition, ReasonCode
from agentic_preflight.shell_fingerprints import (
    ShellInputContract,
    classify_shell,
    compute_shell_fingerprint,
)
from tests.conftest import commit_all, git, write


def _capture(repo, **overrides):
    args = {
        "base_sha": "main",
        "head_sha": "HEAD",
        "command": f'"{sys.executable}" -c "print(1)"',
        "contract": ShellInputContract(mode="content", files=[], environment=[], toolchain=[]),
        "execution_config": {"timeout_seconds": 60},
        "copied_files": [],
        "environment": {},
    }
    args.update(overrides)
    return compute_shell_fingerprint(repo, **args)


def test_history_only_restack_preserves_declared_shell_inputs(feature_repo):
    before = _capture(feature_repo)
    old_base = git("rev-parse", "main", cwd=feature_repo)
    tree = git("rev-parse", "main^{tree}", cwd=feature_repo)
    new_base = git("commit-tree", tree, "-p", old_base, "-m", "history only", cwd=feature_repo)
    git("update-ref", "refs/heads/main", new_base, old_base, cwd=feature_repo)
    git("rebase", "main", cwd=feature_repo)
    after = _capture(feature_repo)
    assert before.inputs_sha256 is not None
    assert classify_shell(before, after).disposition == Disposition.REUSABLE


def test_history_sensitive_command_without_contract_is_unknown(feature_repo):
    value = _capture(feature_repo, command="git log -1", contract=None)
    assert classify_shell(value, value).reasons == (ReasonCode.CONTRACT_UNDECLARED,)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"command": f'"{sys.executable}" -c "print(2)"'}, ReasonCode.COMMAND_CHANGED),
        ({"execution_config": {"timeout_seconds": 30}}, ReasonCode.CONFIG_CHANGED),
    ],
)
def test_command_and_policy_changes_invalidate_the_stage(feature_repo, overrides, reason):
    before = _capture(feature_repo)
    after = _capture(feature_repo, **overrides)
    assert classify_shell(before, after).reasons == (reason,)


def test_ignored_input_changes_are_detected_without_exposing_values(feature_repo):
    contract = ShellInputContract(
        mode="content", files=[".env"], environment=["TOKEN"], toolchain=[]
    )
    write(feature_repo, ".env", "TOKEN=private-before\n")
    before = _capture(feature_repo, contract=contract, environment={"TOKEN": "secret-before"})
    write(feature_repo, ".env", "TOKEN=private-after!\n")
    after = _capture(feature_repo, contract=contract, environment={"TOKEN": "secret-before"})
    assert classify_shell(before, after).reasons == (ReasonCode.INPUTS_CHANGED,)
    env_changed = _capture(feature_repo, contract=contract, environment={"TOKEN": "secret-after!"})
    assert classify_shell(after, env_changed).reasons == (ReasonCode.INPUTS_CHANGED,)
    for value in (before, after, env_changed):
        assert "secret-" not in value.model_dump_json()
        assert "private-" not in value.model_dump_json()
        assert "TOKEN" not in value.model_dump_json()


def test_toolchain_file_changes_invalidate_the_stage(feature_repo, tmp_path):
    tool = tmp_path / "runtime-library"
    tool.write_bytes(b"first")
    contract = ShellInputContract(mode="content", files=[], environment=[], toolchain=[str(tool)])
    before = _capture(feature_repo, contract=contract)
    tool.write_bytes(b"other")
    after = _capture(feature_repo, contract=contract)
    assert classify_shell(before, after).reasons == (ReasonCode.INPUTS_CHANGED,)


@pytest.mark.parametrize("command", ["echo a && echo b", "cd .; echo done"])
def test_login_shell_inputs_are_unsupported(feature_repo, command):
    value = _capture(feature_repo, command=command)
    assert classify_shell(value, value).disposition == Disposition.UNKNOWN


def test_undeclared_copied_file_blocks_reuse(feature_repo):
    value = _capture(feature_repo, copied_files=[".env"])
    assert classify_shell(value, value).reasons == (ReasonCode.INPUTS_UNAVAILABLE,)


def test_dirty_checkout_blocks_reuse(feature_repo):
    write(feature_repo, "new.py", "print(1)\n")
    value = _capture(feature_repo)
    assert classify_shell(value, value).disposition == Disposition.UNKNOWN


def test_missing_input_and_empty_input_differ(feature_repo):
    write(feature_repo, ".gitignore", "local-input\n")
    commit_all(feature_repo, "ignore local input")
    contract = ShellInputContract(
        mode="content", files=["local-input"], environment=[], toolchain=[]
    )
    before = _capture(feature_repo, contract=contract)
    write(feature_repo, "local-input", "")
    after = _capture(feature_repo, contract=contract)
    assert classify_shell(before, after).reasons == (ReasonCode.INPUTS_CHANGED,)


@pytest.mark.parametrize(
    "path", ["../secret", "/secret", ".git/config", "docs/../x", "*.lock", "a\\b", "C:/x"]
)
def test_contract_rejects_nonliteral_or_escaping_input_paths(path):
    with pytest.raises(ValidationError):
        ShellInputContract(mode="content", files=[path], environment=[], toolchain=[])


def test_absent_and_unsupported_evidence_is_unknown(feature_repo):
    value = _capture(feature_repo)
    assert classify_shell(None, value).reasons == (ReasonCode.FINGERPRINT_MISSING,)
    future = value.model_copy(update={"version": 999})
    assert classify_shell(future, future).reasons == (ReasonCode.FINGERPRINT_VERSION_MISMATCH,)
