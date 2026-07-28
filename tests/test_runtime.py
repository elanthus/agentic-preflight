import json
import stat

import pytest

from agentic_preflight import runtime
from agentic_preflight.stages import shellstage


def test_node_project_without_a_pin_is_reported(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"engines": {"node": ">=24 <25"}})
    )
    info = runtime.inspect_project(tmp_path)
    assert info.node_project is True
    assert info.pin_file is None
    assert ">=24 <25" in info.reason


def test_nvm_pin_is_activated_for_a_non_interactive_stage(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / ".nvmrc").write_text("24\n")
    nvm_dir = tmp_path / "nvm"
    fake_bin = nvm_dir / "fake-bin"
    fake_bin.mkdir(parents=True)
    node = fake_bin / "node"
    node.write_text("#!/bin/sh\necho v24.9.0\n")
    node.chmod(node.stat().st_mode | stat.S_IEXEC)
    (nvm_dir / "nvm.sh").write_text(
        f'nvm() {{ export PATH="{fake_bin}:$PATH"; }}\n'
    )
    monkeypatch.setenv("NVM_DIR", str(nvm_dir))

    prepared = runtime.prepare_command(tmp_path, "node --version")
    result = shellstage.run_stage(tmp_path, prepared.command)
    assert result.exit_code == 0
    assert "v24.9.0" in result.output
    assert prepared.runtime.manager == "nvm"


def test_missing_pinned_manager_fails_instead_of_using_system_node(
    tmp_path, monkeypatch
):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / ".nvmrc").write_text("24\n")
    monkeypatch.setenv("NVM_DIR", str(tmp_path / "missing-nvm"))
    prepared = runtime.prepare_command(tmp_path, "node --version", strict=True)
    result = shellstage.run_stage(tmp_path, prepared.command)
    assert result.exit_code == 127
    assert "nvm" in result.output


def test_tool_versions_selects_asdf_and_reads_the_requested_node(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / ".tool-versions").write_text("python 3.13.1\nnodejs 24.9.0\n")
    monkeypatch.setattr(runtime, "_binary", lambda name: f"/bin/{name}")

    info = runtime.inspect_project(tmp_path)

    assert info.manager == "asdf"
    assert info.pin_file == ".tool-versions"
    assert info.requested == "24.9.0"
    assert info.available is True


@pytest.mark.parametrize(
    "requested, expected",
    [("24.9.0", 24), (">=22 <23", 22), ("latest", None), (None, None)],
)
def test_expected_node_major(requested, expected):
    info = runtime.RuntimeInfo("none", requested=requested)
    assert runtime.expected_node_major(info) == expected


def test_non_strict_runtime_falls_back_when_an_explicit_manager_is_missing(
    tmp_path, monkeypatch
):
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.setattr(runtime, "_binary", lambda _name: None)

    prepared = runtime.prepare_command(
        tmp_path, "node --version", manager="volta", strict=False
    )

    assert prepared.command == "node --version"
    assert prepared.runtime.available is False


def test_probe_node_parses_json_after_manager_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "prepare_command",
        lambda *args, **kwargs: runtime.PreparedCommand(
            "node probe", runtime.RuntimeInfo("none", node_project=True)
        ),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: runtime.subprocess.CompletedProcess(
            args[0], 0, 'manager noise\n{"version":"24.9.0","modules":"137"}\n', ""
        ),
    )

    probe = runtime.probe_node(tmp_path)

    assert probe.available is True
    assert probe.version == "24.9.0"
    assert probe.major == 24
    assert probe.modules_abi == "137"


def test_probe_node_reports_command_failure_and_missing_version(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "prepare_command",
        lambda *args, **kwargs: runtime.PreparedCommand(
            "node probe", runtime.RuntimeInfo("none", node_project=True)
        ),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: runtime.subprocess.CompletedProcess(
            args[0], 127, "", "node unavailable\n"
        ),
    )
    failed = runtime.probe_node(tmp_path)
    assert failed.available is False
    assert failed.reason == "node unavailable"

    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: runtime.subprocess.CompletedProcess(
            args[0], 0, "not json\n", ""
        ),
    )
    missing = runtime.probe_node(tmp_path)
    assert missing.available is False
    assert missing.reason == "node returned no version information"
