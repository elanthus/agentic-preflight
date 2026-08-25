"""The PowerShell installers, which are the Windows install path.

Mirrors ``test_install_script`` assertion for assertion. The two installers are
separate scripts, so nothing but a matching pair of test modules keeps them from
drifting apart in ordering, wording, or the safety pause before uninstalling.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
INSTALLER = ROOT / "install.ps1"
UNINSTALLER = ROOT / "uninstall.ps1"

windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the PowerShell installers are the Windows install path",
)


def stub(path: Path, body: str) -> None:
    """A ``.cmd`` shim standing in for a program on PATH."""
    path.write_text(body, encoding="ascii", newline="\r\n")


def powershell(script: Path, *args: str, env: dict, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def fake_bin(tmp_path: Path, *, uv_body: str, cli_body: str | None = None) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub(bin_dir / "uv.cmd", uv_body)
    if cli_body is not None:
        stub(bin_dir / "agentic-preflight.cmd", cli_body)
    return bin_dir


def environment(bin_dir: Path, **extra: str) -> dict:
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_TOOL_BIN_DIR": str(bin_dir),
        **extra,
    }


UV_STUB = (
    '@echo off\r\necho %* >> "%UV_LOG%"\r\nif "%*"=="tool dir --bin" echo %FAKE_TOOL_BIN_DIR%\r\n'
)
CLI_STUB = '@echo off\r\necho %* >> "%CLI_LOG%"\r\n'


@windows_only
@pytest.mark.parametrize(
    ("agents", "expected"),
    [
        ([], "integrations install codex claude cursor opencode amp"),
        (["codex"], "integrations install codex"),
    ],
)
def test_installer_reinstalls_the_checkout_and_refreshes_selected_skills(
    tmp_path, agents, expected
):
    uv_log = tmp_path / "uv.log"
    cli_log = tmp_path / "cli.log"
    bin_dir = fake_bin(tmp_path, uv_body=UV_STUB, cli_body=CLI_STUB)
    env = environment(bin_dir, UV_LOG=str(uv_log), CLI_LOG=str(cli_log))

    result = powershell(INSTALLER, *agents, env=env)

    assert result.returncode == 0, result.stderr
    assert f"tool install --force --reinstall {ROOT}" in uv_log.read_text(encoding="utf-8")
    assert "tool dir --bin" in uv_log.read_text(encoding="utf-8")
    assert cli_log.read_text(encoding="utf-8").strip() == expected


@windows_only
def test_the_installer_refuses_without_uv(tmp_path):
    """Failing loudly beats a half-installed tool the user believes is working."""
    bin_dir = tmp_path / "empty"
    bin_dir.mkdir()
    env = {**os.environ, "PATH": str(bin_dir)}

    result = powershell(INSTALLER, env=env)

    assert result.returncode != 0
    assert "uv is required" in result.stderr


@windows_only
def test_uninstaller_removes_managed_skills_before_the_uv_tool(tmp_path):
    """Ordering is the point: the skill removes repository state, so it must go last."""
    operations = tmp_path / "operations.log"
    bin_dir = fake_bin(
        tmp_path,
        uv_body=(
            "@echo off\r\n"
            'echo uv %* >> "%OPERATION_LOG%"\r\n'
            'if "%*"=="tool dir --bin" echo %FAKE_TOOL_BIN_DIR%\r\n'
        ),
        cli_body='@echo off\r\necho cli %* >> "%OPERATION_LOG%"\r\n',
    )
    env = environment(bin_dir, OPERATION_LOG=str(operations))

    result = powershell(UNINSTALLER, env=env, stdin="\n")

    assert result.returncode == 0, result.stderr
    assert [line.strip() for line in operations.read_text(encoding="utf-8").splitlines()] == [
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
    assert "exec agentic-preflight hook-check" in result.stdout
    assert "If it is a shared or custom hook, do not delete the file" in result.stdout


@windows_only
def test_uninstaller_still_prints_hook_instructions_when_the_cli_is_already_absent(tmp_path):
    bin_dir = fake_bin(
        tmp_path,
        uv_body='@echo off\r\nif "%*"=="tool dir --bin" echo %FAKE_TOOL_BIN_DIR%\r\n',
    )
    env = environment(bin_dir)

    result = powershell(UNINSTALLER, env=env, stdin="\n")

    assert result.returncode == 0, result.stderr
    assert "no CLI or skills were changed" in result.stdout
    assert "git rev-parse --git-path hooks/pre-push" in result.stdout


@windows_only
def test_uninstaller_refuses_to_continue_until_enter_is_received(tmp_path):
    """An unattended run must not silently uninstall before repositories are cleaned."""
    operations = tmp_path / "operations.log"
    bin_dir = fake_bin(tmp_path, uv_body='@echo off\r\necho %* >> "%OPERATION_LOG%"\r\n')
    env = environment(bin_dir, OPERATION_LOG=str(operations))

    result = powershell(UNINSTALLER, env=env, stdin="")

    assert result.returncode != 0
    assert "press Enter after project cleanup" in result.stderr
    assert not operations.exists()


def test_every_installer_defaults_to_the_supported_integrations():
    """Four hardcoded agent lists across two languages, with nothing else linking them.

    Platform-independent on purpose: a contributor adding an integration will most
    likely be on one platform, and the list they forget is the one they cannot run.
    """
    from agentic_preflight import integrations

    expected = set(integrations.SUPPORTED_INTEGRATIONS)
    scripts = {
        "install.sh": ROOT / "install.sh",
        "uninstall.sh": ROOT / "uninstall.sh",
        "install.ps1": INSTALLER,
        "uninstall.ps1": UNINSTALLER,
    }

    for name, path in scripts.items():
        text = path.read_text(encoding="utf-8")
        missing = {agent for agent in expected if agent not in text}
        assert not missing, f"{name} does not offer {sorted(missing)}"


def test_the_powershell_installers_ship_in_the_source_distribution():
    """Platform-independent: the sdist must carry the Windows install path too."""
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    assert "/install.ps1" in includes
    assert "/uninstall.ps1" in includes
