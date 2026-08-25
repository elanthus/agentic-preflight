import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
INSTALLER = ROOT / "install.sh"
UNINSTALLER = ROOT / "uninstall.sh"

# The bash installers are the POSIX install path and cannot run on a stock
# Windows machine. Its equivalent coverage lives in
# ``test_install_script_windows``, against install.ps1 and uninstall.ps1.
posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the bash installers are the POSIX install path",
)


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.mark.parametrize(
    ("agents", "expected"),
    [
        ([], "integrations install codex claude cursor opencode amp"),
        (["codex"], "integrations install codex"),
    ],
)
@posix_only
def test_installer_reinstalls_the_checkout_and_refreshes_selected_skills(
    tmp_path, agents, expected
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_log = tmp_path / "uv.log"
    cli_log = tmp_path / "cli.log"

    _executable(
        bin_dir / "uv",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$UV_LOG"\n'
        'if [ "$*" = "tool dir --bin" ]; then printf \'%s\\n\' "$FAKE_TOOL_BIN_DIR"; fi\n',
    )
    _executable(
        bin_dir / "agentic-preflight",
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CLI_LOG"\n',
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UV_LOG": str(uv_log),
        "CLI_LOG": str(cli_log),
        "FAKE_TOOL_BIN_DIR": str(bin_dir),
    }

    result = subprocess.run(
        [str(INSTALLER), *agents],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"tool install --force --reinstall {ROOT}" in uv_log.read_text(encoding="utf-8")
    assert "tool dir --bin" in uv_log.read_text(encoding="utf-8")
    assert cli_log.read_text(encoding="utf-8").strip() == expected


@posix_only
def test_uninstaller_removes_managed_skills_before_the_uv_tool(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    operation_log = tmp_path / "operations.log"

    _executable(
        bin_dir / "uv",
        "#!/bin/sh\n"
        'printf \'uv %s\\n\' "$*" >> "$OPERATION_LOG"\n'
        'if [ "$*" = "tool dir --bin" ]; then printf \'%s\\n\' "$FAKE_TOOL_BIN_DIR"; fi\n',
    )
    _executable(
        bin_dir / "agentic-preflight",
        '#!/bin/sh\nprintf \'cli %s\\n\' "$*" >> "$OPERATION_LOG"\n',
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "OPERATION_LOG": str(operation_log),
        "FAKE_TOOL_BIN_DIR": str(bin_dir),
    }

    result = subprocess.run(
        [str(UNINSTALLER)],
        cwd=tmp_path,
        env=env,
        input="\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    assert operations == [
        "uv tool dir --bin",
        "cli integrations uninstall codex claude cursor opencode amp",
        "uv tool uninstall agentic-preflight",
    ]
    assert "agentic-preflight:uninstall" in result.stdout
    assert result.stdout.index("agentic-preflight:uninstall") < result.stdout.index(
        "Removing managed agent skills"
    )
    assert "Run history and attestations were left intact" in result.stdout
    assert "git rev-parse --git-path hooks/pre-push" in result.stdout
    assert "Installed by" in result.stdout
    assert "exec agentic-preflight hook-check" in result.stdout
    assert 'rm -- "$hook_path"' in result.stdout
    assert "If it is a shared or custom hook, do not delete the file" in result.stdout


@posix_only
def test_uninstaller_still_prints_hook_instructions_when_the_cli_is_already_absent(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "uv",
        "#!/bin/sh\n"
        'if [ "$*" = "tool dir --bin" ]; then printf \'%s\\n\' "$FAKE_TOOL_BIN_DIR"; fi\n',
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_TOOL_BIN_DIR": str(bin_dir),
    }

    result = subprocess.run(
        [str(UNINSTALLER)],
        cwd=tmp_path,
        env=env,
        input="\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "no CLI or skills were changed" in result.stdout
    assert "git rev-parse --git-path hooks/pre-push" in result.stdout
    assert 'rm -- "$hook_path"' in result.stdout


@posix_only
def test_uninstaller_refuses_to_continue_until_enter_is_received(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    operation_log = tmp_path / "operations.log"
    _executable(
        bin_dir / "uv",
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$OPERATION_LOG"\n',
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "OPERATION_LOG": str(operation_log),
    }

    result = subprocess.run(
        [str(UNINSTALLER)],
        cwd=tmp_path,
        env=env,
        input="",
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "press Enter after project cleanup" in result.stderr
    assert not operation_log.exists()


def test_installers_are_executable_and_included_in_the_source_distribution():
    import tomllib

    assert os.access(INSTALLER, os.X_OK)
    assert os.access(UNINSTALLER, os.X_OK)
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "/install.sh" in includes
    assert "/uninstall.sh" in includes
