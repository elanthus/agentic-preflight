"""Deciding *how* to execute a configured stage command.

``[commands] lint = "ruff check ."`` is stored as an opaque string, and the
historical implementation handed every one of them to ``bash -lc``. That made a
POSIX shell a hard runtime dependency for a tool whose actual job is running a
project's own lint and test commands — the overwhelming majority of which are a
plain program and its arguments with no shell grammar in sight.

So the string is planned before it is run:

* No unquoted shell metacharacter and a resolvable program -> run the argv
  directly, with no shell on any platform.
* Anything else (pipes, ``&&``, redirection, globs, expansions, a shell
  builtin) -> fall back to a shell, exactly as before.

Direct execution is not merely a portability trick. It removes the shell from
the injection surface of the one code path that runs repository-controlled
strings, and it removes the intermediate shell process that otherwise sits
between the timeout and the program whose exit code decides the stage.

Detection is deliberately conservative: when in doubt, use the shell. A false
"needs a shell" costs a subprocess. A false "safe to split" would silently run a
*different command* than the repository asked for, which is the class of quiet
wrongness this tool exists to prevent.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

# Characters that make a shell do something other than split words. Detected
# only outside quotes, except for the two that a shell still expands inside
# double quotes.
_METACHARACTERS = frozenset("|&;<>()$`*?[]{}~#\n")
_EXPAND_IN_DOUBLE_QUOTES = frozenset("$`")

_WSL_SHIM = "system32"

# Whether a backslash escapes the following character.
#
# This is not a stylistic choice. POSIX word-splitting eats backslashes, so
# ``C:\Users\me\tool.exe`` splits into ``C:Usersmetool.exe`` — a *different
# program*, chosen silently. On Windows a backslash is a path separator and is
# kept literal; on POSIX it is an escape and is honoured.
BACKSLASH_ESCAPES = os.name != "nt"


class ShellUnavailable(RuntimeError):
    """A command needs a shell and no usable one was found.

    Carries the reason the shell was needed so the message can name both halves
    of the problem: the metacharacter that forced the fallback and the missing
    interpreter.
    """

    def __init__(self, command: str, reason: str) -> None:
        super().__init__(
            f"cannot run {command!r}: it {reason}, which requires a POSIX shell, "
            "and none was found. Install Git for Windows (which provides bash), or "
            "rewrite the command as a single program and its arguments."
        )
        self.command = command
        self.reason = reason


@dataclass(frozen=True)
class Plan:
    """How ``command`` will actually be handed to the operating system."""

    command: str
    argv: list[str]
    uses_shell: bool
    reason: str | None = None
    """Why a shell was required. ``None`` for direct execution."""


def first_metacharacter(command: str, *, escapes: bool | None = None) -> str | None:
    """The first shell-significant character outside quoting, if any.

    Quote tracking matters: ``pytest -k "foo bar"`` is a perfectly ordinary
    argv, while ``pytest -k "$PATTERN"`` is not, because a shell expands ``$``
    and backticks inside double quotes too.

    ``escapes`` overrides :data:`BACKSLASH_ESCAPES` and exists so both
    conventions stay testable from either platform.
    """
    if escapes is None:
        escapes = BACKSLASH_ESCAPES

    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]

        if escapes and char == "\\" and quote != "'" and index + 1 < len(command):
            # A backslash escapes the next character everywhere a shell would
            # honour it, so neither character is significant here.
            index += 2
            continue

        # Single quotes suppress everything; double quotes suppress everything
        # except the two forms of expansion a shell still performs inside them.
        if quote is None:
            significant = _METACHARACTERS
        elif quote == '"':
            significant = _EXPAND_IN_DOUBLE_QUOTES
        else:
            significant = frozenset()

        if quote is None and char in {'"', "'"}:
            quote = char
        elif quote is not None and char == quote:
            quote = None
        elif char in significant:
            return char

        index += 1
    return None


def split(command: str, *, escapes: bool | None = None) -> list[str] | None:
    """Word-split ``command``, or ``None`` when the quoting does not parse.

    ``shlex.split`` is POSIX-only in a way that matters: it always treats a
    backslash as an escape. The lexer is configured directly so that behaviour
    can be switched off where a backslash means "directory separator".
    """
    if escapes is None:
        escapes = BACKSLASH_ESCAPES

    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    # Comments are handled by metacharacter detection, which reports '#' with a
    # reason rather than silently truncating the command here.
    lexer.commenters = ""
    if not escapes:
        lexer.escape = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _ambiguous_backslash(command: str, escapes: bool) -> bool:
    """A backslash before a quote, where the two conventions disagree.

    POSIX reads ``\\"`` as a literal quote; Windows argument parsing does too,
    but the lexer that keeps ``C:\\path`` intact cannot. Rather than pick a
    reading and be silently wrong, hand the string to a shell.
    """
    if escapes:
        return False
    return any(
        command[index] == "\\" and command[index + 1] in {'"', "'"}
        for index in range(len(command) - 1)
    )


def _leading_assignment(argv: list[str]) -> bool:
    """``FOO=1 pytest`` is shell grammar, not a program named ``FOO=1``."""
    if not argv:
        return False
    name, separator, _ = argv[0].partition("=")
    return bool(separator) and name.isidentifier()


def _shell_reason(command: str, argv: list[str] | None, escapes: bool) -> str | None:
    """Why ``command`` cannot be split into an argv, in words fit for a user."""
    found = first_metacharacter(command, escapes=escapes)
    if found is not None:
        label = "a newline" if found == "\n" else f"the shell metacharacter {found!r}"
        return f"contains {label}"
    if argv is None:
        return "has unbalanced quoting"
    if not argv:
        return "is empty"
    if _ambiguous_backslash(command, escapes):
        return "escapes a quote with a backslash, which is ambiguous on this platform"
    if _leading_assignment(argv):
        return "begins with a shell variable assignment"
    return None


def resolve_program(program: str, cwd: Path | str) -> str | None:
    """Absolute path to ``program``, or ``None`` when it is not an executable.

    Relative program paths are resolved against ``cwd`` rather than the calling
    process's directory. On POSIX that only tidies the result, because the child
    ``chdir``s before ``exec``. On Windows it is required for correctness:
    ``CreateProcess`` resolves a relative path against the *parent's* directory,
    so a relative command would otherwise be looked up in the wrong tree.

    ``shutil.which`` is what makes a bare name work on Windows at all: the
    installed entry point for ``npm``, ``uv``, or ``just`` is commonly a
    ``.cmd`` shim, and ``CreateProcess`` does not apply ``PATHEXT`` on its own.
    """
    if os.path.dirname(program):
        candidate = Path(program)
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        return shutil.which(str(candidate))

    resolved = shutil.which(program)
    if resolved is None:
        return None
    return os.path.abspath(resolved)


def plan(command: str, *, cwd: Path | str, escapes: bool | None = None) -> Plan:
    """Decide whether ``command`` runs as an argv or through a shell.

    A program that does not resolve falls back to the shell rather than
    failing: it is most likely a shell builtin such as ``cd`` or ``source``,
    and the shell is the component entitled to say whether it exists.
    """
    if escapes is None:
        escapes = BACKSLASH_ESCAPES

    argv = split(command, escapes=escapes)
    reason = _shell_reason(command, argv, escapes)
    if reason is not None or argv is None:
        return Plan(command=command, argv=[], uses_shell=True, reason=reason)

    resolved = resolve_program(argv[0], cwd)
    if resolved is None:
        return Plan(
            command=command,
            argv=[],
            uses_shell=True,
            reason=f"names {argv[0]!r}, which is not an executable on PATH",
        )

    return Plan(command=command, argv=[resolved, *argv[1:]], uses_shell=False)


def _windows_bash_candidates() -> list[Path]:
    """Git for Windows bash locations, most trustworthy first."""
    candidates: list[Path] = []

    git = shutil.which("git")
    if git is not None:
        # .../Git/cmd/git.exe and .../Git/bin/git.exe both sit one level below
        # the install root that also contains bin/bash.exe.
        install_root = Path(git).resolve().parent.parent
        candidates.append(install_root / "bin" / "bash.exe")
        candidates.append(install_root / "usr" / "bin" / "bash.exe")

    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(base) / "Programs" / "Git" / "bin" / "bash.exe")

    found = shutil.which("bash")
    if found is not None:
        candidates.append(Path(found))

    return candidates


def find_shell() -> list[str] | None:
    """The shell prefix to run a command string with, or ``None`` if absent.

    On Windows this must never be whatever ``bash`` PATH happens to resolve to:
    a default Windows 11 install puts the *WSL launcher* at
    ``C:\\Windows\\System32\\bash.exe``. Running a stage through it would execute
    the command inside a Linux distribution against a different filesystem, so
    the search is anchored on Git for Windows — already a hard dependency of
    this tool — and any System32 candidate is rejected.
    """
    if os.name != "nt":
        return ["bash", "-lc"]

    for candidate in _windows_bash_candidates():
        if candidate.is_file() and _WSL_SHIM not in str(candidate).lower():
            return [str(candidate), "-lc"]
    return None


def build_argv(command_plan: Plan) -> list[str]:
    """The final argv to hand to ``subprocess``, raising if a shell is missing."""
    if not command_plan.uses_shell:
        return command_plan.argv

    shell = find_shell()
    if shell is None:
        raise ShellUnavailable(command_plan.command, command_plan.reason or "requires a shell")
    return [*shell, command_plan.command]
