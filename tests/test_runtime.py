import json
import stat

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
