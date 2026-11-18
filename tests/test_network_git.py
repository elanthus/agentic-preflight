"""Git commands that contact a remote cannot prompt, and a timeout stops them (#348).

A fetch or push that waits on a credential or host-key prompt, or on a remote that
never answers, holds the clone-wide ``sync`` and ``notes`` locks. Every other
worktree's ``start``, ``mergeback``, and ``push`` then blocks on those locks.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_preflight import filelock, gitx, runs
from agentic_preflight import store as storemod
from agentic_preflight.envelope import ExitCode
from tests.conftest import configure_base, git, held_popen_prelude
from tests.driver import ScriptedAgent, findings_json

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the remote helpers here are POSIX shell scripts"
)


def _remote_helper(tmp_path: Path, monkeypatch, name: str, body: str) -> str:
    """Install ``git-remote-<name>`` on PATH and return a URL that selects it."""
    bin_dir = tmp_path / "helper-bin"
    bin_dir.mkdir(exist_ok=True)
    helper = bin_dir / f"git-remote-{name}"
    helper.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    return f"{name}::unused"


_ASK_FOR_A_CREDENTIAL = (
    "printf 'protocol=https\\nhost=example.invalid\\n\\n' "
    '| git credential fill > "$AP_TEST_OUT" 2>&1\n'
    "exit 1\n"
)


@posix_only
def test_a_remote_command_cannot_prompt_for_a_credential(tmp_repo, tmp_path, monkeypatch):
    url = _remote_helper(tmp_path, monkeypatch, "askcred", _ASK_FOR_A_CREDENTIAL)
    out = tmp_path / "credential.out"
    monkeypatch.setenv("AP_TEST_OUT", str(out))
    # Even a user who turned terminal prompts on explicitly cannot block the tool.
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    result = gitx.run(tmp_repo, "fetch", url, check=False, remote="origin", timeout=60)

    assert result.returncode != 0
    assert "terminal prompts disabled" in out.read_text(encoding="utf-8")


@posix_only
def test_a_remote_command_still_uses_a_configured_credential_helper(
    tmp_repo, tmp_path, monkeypatch
):
    url = _remote_helper(tmp_path, monkeypatch, "askcred", _ASK_FOR_A_CREDENTIAL)
    out = tmp_path / "credential.out"
    monkeypatch.setenv("AP_TEST_OUT", str(out))
    git(
        "config",
        "credential.helper",
        "!f() { echo username=stored-user; echo password=stored-secret; }; f",
        cwd=tmp_repo,
    )

    gitx.run(tmp_repo, "fetch", url, check=False, remote="origin", timeout=60)

    answer = out.read_text(encoding="utf-8")
    assert "username=stored-user" in answer
    assert "password=stored-secret" in answer


@posix_only
def test_a_remote_command_has_no_controlling_terminal(tmp_repo, tmp_path, monkeypatch):
    # SSH reads host-key answers and key passphrases from /dev/tty, not stdin.
    url = _remote_helper(
        tmp_path,
        monkeypatch,
        "ttycheck",
        'if (exec </dev/tty) 2>/dev/null; then echo tty; else echo none; fi > "$AP_TEST_OUT"\n'
        "exit 1\n",
    )
    out = tmp_path / "tty.out"
    monkeypatch.setenv("AP_TEST_OUT", str(out))

    gitx.run(tmp_repo, "fetch", url, check=False, remote="origin", timeout=60)

    assert out.read_text(encoding="utf-8").strip() == "none"


@posix_only
def test_a_stalled_remote_times_out_and_its_processes_stop(tmp_repo, tmp_path, monkeypatch):
    url = _remote_helper(
        tmp_path, monkeypatch, "stall", 'echo $$ > "$AP_TEST_OUT"\nexec sleep 120\n'
    )
    pid_file = tmp_path / "stall.pid"
    monkeypatch.setenv("AP_TEST_OUT", str(pid_file))

    started = time.monotonic()
    with pytest.raises(gitx.RemoteTimeout) as info:
        gitx.run(tmp_repo, "fetch", url, remote="origin", timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 30
    assert isinstance(info.value, gitx.GitError)
    assert info.value.remote == "origin"
    assert info.value.operation == "fetch"
    assert info.value.timeout == 1
    message = str(info.value)
    assert "origin" in message
    assert "credentials" in message
    assert "network" in message
    # The helper Git started is stopped with Git, not left running.
    assert _process_stops(int(pid_file.read_text(encoding="utf-8")))
    # Git was asked to stop before it was killed, so it left no lock file behind.
    assert sorted((tmp_repo / ".git").rglob("*.lock")) == []


def _process_stops(pid: int, within: float = 10) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@posix_only
@pytest.mark.parametrize(
    "signum", [signal.SIGTERM, signal.SIGHUP] if hasattr(signal, "SIGHUP") else []
)
def test_a_signal_to_the_tool_stops_a_running_remote_command(
    tmp_repo, tmp_path, monkeypatch, signum
):
    url = _remote_helper(
        tmp_path, monkeypatch, "stall", 'echo $$ > "$AP_TEST_OUT"\nexec sleep 120\n'
    )
    pid_file = tmp_path / "stall.pid"
    monkeypatch.setenv("AP_TEST_OUT", str(pid_file))
    script = (
        "import sys\n"
        "from agentic_preflight import gitx\n"
        "gitx.run(sys.argv[1], 'fetch', sys.argv[2], remote='origin')\n"
    )
    tool = subprocess.Popen([sys.executable, "-c", script, str(tmp_repo), url])
    try:
        deadline = time.monotonic() + 30
        while not pid_file.exists() or not pid_file.read_text(encoding="utf-8").strip():
            assert time.monotonic() < deadline, "the remote helper never started"
            assert tool.poll() is None, "the tool exited before the helper started"
            time.sleep(0.05)
        helper_pid = int(pid_file.read_text(encoding="utf-8"))

        tool.send_signal(signum)
        tool.wait(timeout=30)
    finally:
        if tool.poll() is None:
            tool.kill()

    assert tool.returncode == -signum
    assert _process_stops(helper_pid)


@posix_only
def test_a_signal_that_arrives_while_a_remote_command_is_starting_still_stops_it(
    tmp_repo, tmp_path, monkeypatch
):
    url = _remote_helper(
        tmp_path, monkeypatch, "stall", 'echo $$ > "$AP_TEST_OUT"\nexec sleep 120\n'
    )
    pid_file = tmp_path / "stall.pid"
    release = tmp_path / "release"
    monkeypatch.setenv("AP_TEST_OUT", str(pid_file))
    script = (
        held_popen_prelude(release) + "import sys\n"
        "from agentic_preflight import gitx\n"
        "gitx.run(sys.argv[1], 'fetch', sys.argv[2], remote='origin')\n"
    )
    tool = subprocess.Popen([sys.executable, "-c", script, str(tmp_repo), url])
    try:
        deadline = time.monotonic() + 30
        while not pid_file.exists() or not pid_file.read_text(encoding="utf-8").strip():
            assert time.monotonic() < deadline, "the remote helper never started"
            assert tool.poll() is None, "the tool exited before the helper started"
            time.sleep(0.05)
        helper_pid = int(pid_file.read_text(encoding="utf-8"))

        tool.send_signal(signal.SIGTERM)
        release.touch()
        tool.wait(timeout=30)
    finally:
        if tool.poll() is None:
            tool.kill()

    assert tool.returncode == -signal.SIGTERM
    assert _process_stops(helper_pid)


@posix_only
@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX hangup signal")
def test_an_ignored_hangup_leaves_a_running_remote_command_alone(tmp_repo, tmp_path, monkeypatch):
    url = _remote_helper(
        tmp_path, monkeypatch, "stall", 'echo $$ > "$AP_TEST_OUT"\nexec sleep 120\n'
    )
    pid_file = tmp_path / "stall.pid"
    monkeypatch.setenv("AP_TEST_OUT", str(pid_file))
    script = (
        "import signal, sys\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "from agentic_preflight import gitx\n"
        "gitx.run(sys.argv[1], 'fetch', sys.argv[2], remote='origin')\n"
    )
    tool = subprocess.Popen([sys.executable, "-c", script, str(tmp_repo), url])
    try:
        deadline = time.monotonic() + 30
        while not pid_file.exists() or not pid_file.read_text(encoding="utf-8").strip():
            assert time.monotonic() < deadline, "the remote helper never started"
            assert tool.poll() is None, "the tool exited before the helper started"
            time.sleep(0.05)
        helper_pid = int(pid_file.read_text(encoding="utf-8"))

        tool.send_signal(signal.SIGHUP)
        time.sleep(1)

        assert tool.poll() is None
        os.kill(helper_pid, 0)  # still running: raises if the hangup stopped it
    finally:
        tool.kill()
        tool.wait(timeout=30)
        with contextlib.suppress(ProcessLookupError, UnboundLocalError):
            os.kill(helper_pid, signal.SIGKILL)


@posix_only
def test_a_remote_command_does_not_wait_on_stdin(tmp_repo, tmp_path, monkeypatch):
    # ``cat`` returns at once on an empty stdin; on an inherited terminal it would wait.
    script = tmp_path / "read_stdin.sh"
    script.write_text(f"#!/bin/sh\ncat > '{tmp_path / 'stdin.out'}'\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    git("config", "alias.readstdin", f"!{script}", cwd=tmp_repo)
    reader, writer = os.pipe()
    try:
        # Hand the tool a stdin that never ends, as a terminal would be.
        saved = os.dup(0)
        os.dup2(reader, 0)
        try:
            started = time.monotonic()
            result = gitx.run(tmp_repo, "readstdin", check=False, remote="origin", timeout=30)
            elapsed = time.monotonic() - started
        finally:
            os.dup2(saved, 0)
            os.close(saved)
    finally:
        os.close(reader)
        os.close(writer)

    assert elapsed < 20
    assert result.returncode == 1
    assert (tmp_path / "stdin.out").read_text(encoding="utf-8") == ""


def _ssh_command_seen(tmp_repo, tmp_path, monkeypatch) -> str:
    url = _remote_helper(
        tmp_path,
        monkeypatch,
        "sshenv",
        'printf "%s" "${GIT_SSH_COMMAND-<unset>}" > "$AP_TEST_OUT"\nexit 1\n',
    )
    out = tmp_path / "ssh.out"
    monkeypatch.setenv("AP_TEST_OUT", str(out))
    gitx.run(tmp_repo, "fetch", url, check=False, remote="origin", timeout=60)
    return out.read_text(encoding="utf-8")


@posix_only
def test_ssh_runs_in_batch_mode_when_the_user_chose_no_ssh_command(tmp_repo, tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)

    assert _ssh_command_seen(tmp_repo, tmp_path, monkeypatch) == gitx.BATCH_SSH_COMMAND


@posix_only
@pytest.mark.parametrize("source", ["GIT_SSH_COMMAND", "GIT_SSH", "core.sshCommand"])
def test_a_users_ssh_command_is_left_alone(tmp_repo, tmp_path, monkeypatch, source):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    if source == "core.sshCommand":
        git("config", "core.sshCommand", "ssh -i /custom/key", cwd=tmp_repo)
        expected = "<unset>"
    else:
        monkeypatch.setenv(source, "my-ssh")
        expected = "my-ssh" if source == "GIT_SSH_COMMAND" else "<unset>"

    assert _ssh_command_seen(tmp_repo, tmp_path, monkeypatch) == expected


def test_a_remote_timeout_message_carries_no_credentials():
    error = gitx.RemoteTimeout(
        ["fetch", "origin"],
        remote="https://user:secret-token@example.invalid/repo.git",
        timeout=5,
        stderr="",
    )
    assert "secret-token" not in str(error)
    assert "secret-token" not in error.remote


def test_a_remote_name_is_reported_as_given():
    error = gitx.RemoteTimeout(["fetch", "up@stream"], remote="up@stream", timeout=5, stderr="")
    assert error.remote == "up@stream"
    assert "pre-push" not in str(error)


def test_a_push_timeout_says_it_includes_the_pre_push_hook():
    error = gitx.RemoteTimeout(["push", "origin"], remote="origin", timeout=5, stderr="")
    assert "pre-push hook" in str(error)


# -- start ---------------------------------------------------------------------


def _stall_fetch(monkeypatch, seen: list[dict]) -> None:
    real_run = gitx.run

    def stalled(cwd, *args, **kwargs):
        if args and args[0] == "fetch":
            seen.append(kwargs)
            raise gitx.RemoteTimeout(list(args), remote=kwargs["remote"], timeout=7, stderr="")
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(gitx, "run", stalled)


def _lock_is_free(store: storemod.Store, name: str) -> bool:
    with filelock.try_exclusive(store.root / "resources" / f"{name}.lock") as acquired:
        return acquired


def test_start_reports_a_remote_timeout_and_releases_the_locks(
    agent, feature_repo, bare_remote, monkeypatch
):
    seen: list[dict] = []
    _stall_fetch(monkeypatch, seen)

    env = agent.run("start", expect=ExitCode.NEEDS_HUMAN)

    assert seen
    assert seen[0]["remote"] == "origin"
    assert env["error"]["code"] == "remote_timeout"
    assert "origin" in env["error"]["message"]
    assert env["state"] == "SYNC_CONFLICT"
    assert env["data"]["remote"] == "origin"
    assert env["data"]["operation"] == "fetch"
    assert env["data"]["timeout_seconds"] == 7
    assert env["next"]["command"] == "agentic-preflight abort --force"
    store = runs.open_session(feature_repo).store
    assert _lock_is_free(store, "sync")
    assert _lock_is_free(store, "notes")
    assert "remote_timeout" in store.events_path(env["run_id"]).read_text(encoding="utf-8")
    assert agent.run("abort", "--force")["state"] == "ABORTED"


def test_start_fetches_the_base_and_notes_without_prompting(
    agent, feature_repo, bare_remote, monkeypatch
):
    real_run = gitx.run
    remote_calls: list[tuple[str, str | None, float | None]] = []

    def recording(cwd, *args, **kwargs):
        if args and args[0] in {"fetch", "push", "ls-remote"}:
            remote_calls.append((args[0], kwargs.get("remote"), kwargs.get("timeout")))
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(gitx, "run", recording)

    agent.run("start")

    assert remote_calls == [("fetch", "origin", 600), ("fetch", "origin", 600)]


@posix_only
def test_start_against_a_stalled_remote_fails_within_the_configured_timeout(
    agent, feature_repo, bare_remote, tmp_path, monkeypatch
):
    configure_base(feature_repo, "[general]\nnetwork_timeout_seconds = 1\n")
    url = _remote_helper(
        tmp_path, monkeypatch, "stall", 'echo $$ > "$AP_TEST_OUT"\nexec sleep 120\n'
    )
    monkeypatch.setenv("AP_TEST_OUT", str(tmp_path / "stall.pid"))
    git("remote", "set-url", "origin", url, cwd=feature_repo)

    started = time.monotonic()
    env = agent.run("start", expect=ExitCode.NEEDS_HUMAN)
    elapsed = time.monotonic() - started

    assert elapsed < 60
    assert env["error"]["code"] == "remote_timeout"
    assert env["data"]["remote"] == "origin"
    assert env["data"]["operation"] == "fetch"
    assert env["data"]["timeout_seconds"] == 1
    assert ".lock" in env["next"]["instruction"]
    store = runs.open_session(feature_repo).store
    assert _lock_is_free(store, "sync")
    assert _lock_is_free(store, "notes")
    assert _process_stops(int((tmp_path / "stall.pid").read_text(encoding="utf-8")))


# -- push ----------------------------------------------------------------------


def test_push_reports_a_remote_timeout_and_keeps_the_run_awaiting_push(
    feature_repo, bare_remote, tmp_path, monkeypatch
):
    configure_base(
        feature_repo,
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"
    token = agent.run("gate")["data"]["token"]

    real_run = gitx.run
    pushes: list[dict] = []

    def stalled(cwd, *args, **kwargs):
        if args and args[0] == "push":
            pushes.append(kwargs)
            raise gitx.RemoteTimeout(list(args), remote=kwargs["remote"], timeout=7, stderr="")
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(gitx, "run", stalled)

    env = agent.run("push", "--confirm", token, expect=ExitCode.NEEDS_HUMAN)

    assert pushes
    assert pushes[0]["remote"] == "origin"
    assert env["error"]["code"] == "remote_timeout"
    assert env["data"]["remote"] == "origin"
    assert env["data"]["operation"] == "push"
    assert env["data"]["timeout_seconds"] == 7
    assert env["run_id"]
    assert env["state"] == "AWAITING_PUSH_CONFIRM"
    assert "may have landed" in env["next"]["instruction"]
    assert "pre-push hook" in env["next"]["instruction"]
    assert env["next"]["command"] == "agentic-preflight status"
    assert agent.run("status")["state"] == "AWAITING_PUSH_CONFIRM"
    assert _lock_is_free(runs.open_session(feature_repo).store, "notes")


# -- resource locks ------------------------------------------------------------


def test_a_command_waiting_on_a_resource_lock_says_which_run_holds_it(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(storemod, "RESOURCE_WAIT_NOTICE_SECONDS", 0.2)
    store = storemod.Store(tmp_path / "state")
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store.resource("sync", holder="r_holder1"):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(10)
        timer = threading.Timer(1.0, release.set)
        timer.start()
        with store.resource("sync", holder="r_waiter1"):
            pass
    finally:
        release.set()
        thread.join(10)

    notice = capsys.readouterr().err
    assert "'sync'" in notice
    assert "r_holder1" in notice
    assert str(os.getpid()) in notice


def test_an_uncontended_resource_lock_prints_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(storemod, "RESOURCE_WAIT_NOTICE_SECONDS", 0.0)
    store = storemod.Store(tmp_path / "state")
    with store.resource("notes", holder="r_only1"):
        pass
    assert capsys.readouterr().err == ""
