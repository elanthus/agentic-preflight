"""Planning how a configured stage command reaches the operating system.

The classification tests carry the weight here. A command wrongly judged
"needs a shell" only costs a process; a command wrongly judged safe to split
would run something other than what the repository configured.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_preflight.stages import command as command_plan
from agentic_preflight.stages import shellstage

WINDOWS = os.name == "nt"


def executable(tmp_path: Path, name: str = "stage-probe") -> str:
    """A real executable on PATH, named so nothing else could match it."""
    if WINDOWS:
        path = tmp_path / f"{name}.cmd"
        path.write_text("@echo off\r\necho ran\r\n", encoding="ascii")
    else:
        path = tmp_path / name
        path.write_text("#!/bin/sh\necho ran\n", encoding="ascii")
        path.chmod(0o755)
    return name


# -- metacharacter detection ------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "pytest",
        "ruff check .",
        "npm run test",
        "uv run pytest -q --maxfail=1",
        'pytest -k "foo bar"',
        "pytest -k 'foo bar'",
        "cargo test --all-features",
        'git commit -m "it is fine"',
        "python -m pytest tests/unit",
    ],
)
def test_ordinary_commands_carry_no_shell_grammar(command):
    assert command_plan.first_metacharacter(command) is None


@pytest.mark.parametrize(
    ("command", "found"),
    [
        ("ruff check . && mypy", "&"),
        ("pytest; ruff check .", ";"),
        ("pytest | tee out.txt", "|"),
        ("pytest > out.txt", ">"),
        ("pytest < input.txt", "<"),
        ("ruff check *.py", "*"),
        ("ls file?.txt", "?"),
        ("pytest tests/[ab]", "["),
        ("pytest tests/{a,b}", "{"),
        ("cat ~/.config", "~"),
        ("pytest $EXTRA", "$"),
        ("echo `date`", "`"),
        ("(pytest)", "("),
        ("pytest # comment", "#"),
        ("pytest\nruff check .", "\n"),
    ],
)
def test_shell_grammar_is_detected(command, found):
    assert command_plan.first_metacharacter(command) == found


def test_metacharacters_inside_single_quotes_are_literal():
    assert command_plan.first_metacharacter("pytest -k 'a && b | c'") is None


def test_expansions_are_detected_inside_double_quotes():
    """A shell still expands ``$`` and backticks between double quotes."""
    assert command_plan.first_metacharacter('pytest -k "$PATTERN"') == "$"
    assert command_plan.first_metacharacter('echo "`date`"') == "`"


def test_globs_inside_double_quotes_are_literal():
    """Unlike ``$``, a glob is not expanded between double quotes."""
    assert command_plan.first_metacharacter('pytest -k "a*b"') is None


def test_an_escaped_metacharacter_is_literal_where_backslash_escapes():
    assert command_plan.first_metacharacter("pytest -k a\\*b", escapes=True) is None


def test_a_backslash_inside_single_quotes_does_not_escape():
    """``'\\'`` closes nothing: the quote after the backslash is the terminator."""
    assert command_plan.first_metacharacter("pytest -k 'a\\' && mypy", escapes=True) == "&"


# -- backslash conventions --------------------------------------------------


def test_a_windows_path_survives_splitting_unquoted():
    """POSIX splitting would turn this into ``C:Usersmetool.exe`` — another program."""
    argv = command_plan.split(r"C:\Users\me\tool.exe --flag", escapes=False)

    assert argv == [r"C:\Users\me\tool.exe", "--flag"]


def test_posix_splitting_still_honours_escapes():
    assert command_plan.split(r"pytest -k a\ b", escapes=True) == ["pytest", "-k", "a b"]


def test_a_backslash_escaped_quote_is_refused_where_the_conventions_disagree(tmp_path):
    """Neither reading is safe to guess, so the shell gets the string intact."""
    plan = command_plan.plan(r'git commit -m "say \"hi\""', cwd=tmp_path, escapes=False)

    assert plan.uses_shell is True
    assert "ambiguous" in plan.reason


def test_this_platform_uses_the_right_backslash_convention():
    assert command_plan.BACKSLASH_ESCAPES is not WINDOWS


# -- planning ---------------------------------------------------------------


def test_a_plain_command_is_planned_for_direct_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    name = executable(tmp_path)

    plan = command_plan.plan(f"{name} --flag value", cwd=tmp_path)

    assert plan.uses_shell is False
    assert plan.reason is None
    assert Path(plan.argv[0]).is_absolute()
    assert plan.argv[1:] == ["--flag", "value"]


def test_quoted_arguments_survive_the_split(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    name = executable(tmp_path)

    plan = command_plan.plan(f'{name} -k "foo bar"', cwd=tmp_path)

    assert plan.uses_shell is False
    assert plan.argv[1:] == ["-k", "foo bar"]


def test_shell_grammar_falls_back_to_the_shell(tmp_path):
    plan = command_plan.plan("ruff check . && mypy", cwd=tmp_path)

    assert plan.uses_shell is True
    assert plan.argv == []
    assert "'&'" in plan.reason


def test_a_leading_variable_assignment_falls_back_to_the_shell(tmp_path):
    plan = command_plan.plan("CI=1 pytest", cwd=tmp_path)

    assert plan.uses_shell is True
    assert "variable assignment" in plan.reason


def test_unbalanced_quoting_falls_back_to_the_shell(tmp_path):
    """The shell owns the error message for syntax the splitter cannot parse."""
    plan = command_plan.plan("pytest -k 'unterminated", cwd=tmp_path)

    assert plan.uses_shell is True
    assert "unbalanced quoting" in plan.reason


def test_an_empty_command_falls_back_to_the_shell(tmp_path):
    plan = command_plan.plan("   ", cwd=tmp_path)

    assert plan.uses_shell is True
    assert "is empty" in plan.reason


def test_an_unresolvable_program_falls_back_to_the_shell(tmp_path):
    """Most likely a builtin such as ``cd``; the shell decides, not the planner."""
    plan = command_plan.plan("definitely-not-installed-anywhere --flag", cwd=tmp_path)

    assert plan.uses_shell is True
    assert "not an executable on PATH" in plan.reason


def test_a_relative_program_resolves_against_the_worktree(tmp_path, monkeypatch):
    """``CreateProcess`` would resolve it against the caller's directory instead."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    executable(worktree, "local-probe")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    suffix = ".cmd" if WINDOWS else ""
    plan = command_plan.plan(f"./local-probe{suffix}", cwd=worktree)

    assert plan.uses_shell is False
    assert Path(plan.argv[0]).parent == worktree


# -- shell discovery --------------------------------------------------------


def test_the_shell_is_found_on_this_machine():
    """Git provides bash on Windows and is already a hard dependency."""
    assert command_plan.find_shell() is not None


@pytest.mark.skipif(not WINDOWS, reason="the WSL shim only exists on Windows")
def test_the_windows_shell_is_never_the_wsl_shim():
    """``C:\\Windows\\System32\\bash.exe`` launches WSL against another filesystem."""
    shell = command_plan.find_shell()

    assert "system32" not in shell[0].lower()


def test_a_missing_shell_is_reported_with_the_reason_it_was_needed(monkeypatch):
    monkeypatch.setattr(command_plan, "find_shell", lambda: None)
    plan = command_plan.plan("ruff check . && mypy", cwd=".")

    with pytest.raises(command_plan.ShellUnavailable) as caught:
        command_plan.build_argv(plan)

    message = str(caught.value)
    assert "ruff check . && mypy" in message
    assert "'&'" in message


# -- execution --------------------------------------------------------------


def test_a_direct_command_receives_its_arguments_verbatim(tmp_path):
    """Quoting is resolved once, by the splitter, and not re-interpreted."""
    script = tmp_path / "show_argv.py"
    script.write_text("import json, sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
    command = f'"{sys.executable}" show_argv.py -k "foo bar" plain'

    assert command_plan.plan(command, cwd=tmp_path).uses_shell is False
    result = shellstage.run_stage(tmp_path, command)

    assert result.passed
    assert json.loads(result.output) == ["-k", "foo bar", "plain"]


def test_a_shell_command_still_runs_through_the_shell(tmp_path):
    result = shellstage.run_stage(tmp_path, f'"{sys.executable}" -c "print(1)" && echo second')

    assert result.passed
    assert "second" in result.output


def test_the_exit_code_survives_direct_execution(tmp_path):
    result = shellstage.run_stage(tmp_path, f'"{sys.executable}" -c "raise SystemExit(3)"')

    assert result.exit_code == 3
    assert not result.passed


def test_an_unrunnable_command_is_a_red_stage_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(command_plan, "find_shell", lambda: None)

    result = shellstage.run_stage(tmp_path, "ruff check . && mypy")

    assert result.exit_code == shellstage.EXIT_UNRUNNABLE
    assert not result.passed
    assert "requires a POSIX shell" in result.output


def test_a_timeout_kills_a_directly_executed_child(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text("import time; time.sleep(30)", encoding="utf-8")

    result = shellstage.run_stage(tmp_path, f'"{sys.executable}" sleeper.py', timeout_seconds=1)

    assert result.timed_out
    assert result.exit_code == 124


def test_an_unrunnable_setup_command_is_reported_as_a_failed_result(tmp_path, monkeypatch):
    from agentic_preflight import worktree

    monkeypatch.setattr(command_plan, "find_shell", lambda: None)

    result = worktree.run_setup(tmp_path, "uv sync && echo done")

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == shellstage.EXIT_UNRUNNABLE
    assert "requires a POSIX shell" in result.stderr
