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

Two consequences of executing directly are worth stating plainly, because
neither announces itself:

* The login shell profile is not sourced. Where a version manager puts its shims
  on ``PATH`` from that profile, a stage can run the system build of a program
  rather than the managed one. See COMPATIBILITY.md.
* Resolution is PATH-only. A bare program name is never looked up in a working
  directory, so the repository being validated cannot supply the program that
  validates it. See :func:`resolve_on_path`.
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

# Windows' own default when PATHEXT is unset.
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"

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
            "and none was found. Install one (on Windows, Git for Windows provides "
            "bash), or rewrite the command as a single program and its arguments."
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

        if escapes and char == "\\" and quote == '"':
            # Inside double quotes the shell and the splitter disagree. A shell
            # drops the backslash before ``$``, a backtick, a quote, or another
            # backslash and keeps it otherwise; ``shlex`` keeps it in every
            # case. So ``-k "cost\$"`` reaches the program as ``cost$`` through
            # a shell and as ``cost\$`` through an argv — a different command,
            # chosen silently. Hand the whole string to the shell instead.
            return char

        if escapes and char == "\\" and quote is None and index + 1 < len(command):
            # Unquoted, the two agree: the backslash escapes the next character
            # and neither is significant.
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
    if found == "\\":
        return "escapes a character inside double quotes, which a shell reads differently"
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


def _search_path_entries() -> list[str]:
    return [entry for entry in os.environ.get("PATH", os.defpath).split(os.pathsep) if entry]


def _candidate_names(program: str) -> list[str]:
    """The filenames a bare ``program`` could have on this platform.

    Windows executability comes from the extension, and the installed entry
    point for ``npm``, ``uv``, or ``just`` is commonly a ``.cmd`` shim rather
    than an ``.exe``, so ``PATHEXT`` has to be applied here — ``CreateProcess``
    does not apply it on our behalf.
    """
    if os.name != "nt":
        return [program]

    extensions = [
        extension
        for extension in os.environ.get("PATHEXT", _DEFAULT_PATHEXT).split(os.pathsep)
        if extension
    ]
    _, existing = os.path.splitext(program)
    names = [program] if existing else []
    return names + [program + extension for extension in extensions]


def resolve_on_path(program: str) -> str | None:
    """Absolute path to a bare ``program`` name, searching PATH and only PATH.

    Deliberately not ``shutil.which``. On Windows that function prepends the
    *calling process's* current directory to the search, which for this tool is
    the repository under validation — so a repository containing its own
    ``pytest.exe`` or ``ruff.bat`` would have that run in place of the real
    tool. Passing ``path=`` does not suppress it; the directory is inserted
    after the supplied path is split. Whether it happens at all depends on an
    environment variable and on the Python version, which is no basis for
    deciding what gets executed.

    Searching PATH alone also matches what a shell did here before, and what
    ``execvp`` does on POSIX. Returning an absolute path then stops
    ``CreateProcess`` performing its own current-directory search afterwards.
    """
    for directory in _search_path_entries():
        for name in _candidate_names(program):
            candidate = os.path.join(directory, name)
            # POSIX decides executability by mode, Windows by extension — which
            # ``_candidate_names`` has already applied, and where ``X_OK`` is
            # true of every existing file and so proves nothing.
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return os.path.abspath(candidate)
    return None


def resolve_program(program: str, cwd: Path | str) -> str | None:
    """Absolute path to ``program``, or ``None`` when it is not an executable.

    A program *path* is resolved against ``cwd`` rather than the calling
    process's directory. On POSIX that only tidies the result, because the child
    ``chdir``s before ``exec``. On Windows it is required for correctness:
    ``CreateProcess`` resolves a relative path against the *parent's* directory,
    so a relative command would otherwise be looked up in the wrong tree.

    A bare *name* is looked up on PATH instead, never in a working directory.
    See :func:`resolve_on_path`.
    """
    if os.path.dirname(program):
        candidate = Path(program)
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        # A path already names the file, so this only checks it is executable;
        # no directory search happens and the working directory cannot leak in.
        return shutil.which(str(candidate))

    return resolve_on_path(program)


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
    """Git for Windows bash locations, most trustworthy first.

    Resolved with :func:`resolve_on_path`, never ``shutil.which``, for the same
    reason stage programs are: ``which`` searches the calling process's current
    directory — the repository under validation — which must not be able to
    supply the shell that runs its own stages.
    """
    candidates: list[Path] = []

    git = resolve_on_path("git")
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

    found = resolve_on_path("bash")
    if found is not None:
        candidates.append(Path(found))

    return candidates


def windows_system_tool(name: str) -> str:
    """Absolute System32 path for a Windows utility such as ``taskkill.exe``.

    Named in full so ``CreateProcess`` performs no search at all: handed a bare
    name it looks in the current directory — for this tool, the repository
    under validation — before System32.
    """
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(system_root, "System32", name)


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
        # Probed rather than assumed: a minimal container image may ship only
        # ``sh``, and a missing shell must surface as ShellUnavailable — the
        # red stage the caller reports — not as a FileNotFoundError from exec.
        for name in ("bash", "sh"):
            shell = resolve_on_path(name)
            if shell is not None:
                return [shell, "-lc"]
        return None

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
