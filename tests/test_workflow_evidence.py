"""Execute the hosted shell callers with deterministic external-command responses."""

import json
import os
import re
import shlex
import shutil
import subprocess
import textwrap
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

HEAD = "a" * 40
BASE = "b" * 40
BRANCH = "feature/$(touch${IFS}injected)"


def _policy_script(filename):
    workflow = (ROOT / ".github/workflows" / filename).read_text()
    label = (
        "Fetch and verify the pull request attestation"
        if filename == "attestation.yml"
        else "Evaluate approval policy for the exact head"
    )
    end = "\n  environment:"
    stop = len(workflow) if filename == "attestation.yml" else workflow.index(end)
    block = workflow[workflow.index("      - name: " + label) : stop]
    return textwrap.dedent(block.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0])


@pytest.mark.parametrize(
    ("filename", "job", "head_variable"),
    [
        ("attestation.yml", "attestation", "ATTESTED_SHA"),
        ("human-approval.yml", "policy", "HEAD_SHA"),
    ],
)
def test_protected_workflow_configuration(filename, job, head_variable):
    workflow = (ROOT / ".github/workflows" / filename).read_text()
    # Bound assertions to the trusted job; normalize whitespace, not YAML structure.
    trusted = workflow.split(f"\n  {job}:\n", 1)[1].split("\n  environment:\n", 1)[0]
    trusted = " ".join(trusted.split())
    assert "with: ref: ${{ github.event.pull_request.base.sha }} fetch-depth: 0" in trusted
    assert (
        trusted.index("- uses: actions/checkout@")
        < trusted.index("run: uv sync --locked --no-dev --no-install-project --python 3.11")
        < trusted.index("env:")
    )
    for variable, field in {
        head_variable: "head.sha",
        "HEAD_REPOSITORY": "head.repo.full_name",
        "HEAD_REPOSITORY_URL": "head.repo.clone_url",
    }.items():
        assert f"{variable}: ${{{{ github.event.pull_request.{field} }}}}" in trusted
    permissions = workflow.split("\npermissions:\n", 1)[1].split("\njobs:\n", 1)[0]
    assert permissions.split() == (
        ["contents:", "read"]
        if filename == "attestation.yml"
        else ["actions:", "read", "contents:", "read", "pull-requests:", "read"]
    )
    if filename == "attestation.yml":
        events = workflow.split("\non:\n", 1)[1].split("\nconcurrency:", 1)[0]
        assert events.split() == ["pull_request_target:"]
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        assert "attestation:" not in ci.split("\njobs:\n", 1)[1]
        assert "needs.dependabot-policy.outputs.eligible != 'true'" in trusted


TRUSTED_WORKFLOWS = ["attestation.yml", "human-approval.yml"]
LOCKED_SYNC = "uv sync --locked --no-dev --no-install-project --python 3.11"
LOCKED_RUN = "uv run --locked --no-dev --no-sync --python 3.11"
PINNED_SETUP_UV = re.compile(
    r'uses: astral-sh/setup-uv@[0-9a-f]{40} # v\S+ with: version: "\d+\.\d+\.\d+"'
)
# uv options that take their value from the next token.
UV_VALUE_OPTIONS = {
    "--python",
    "-p",
    "--directory",
    "--project",
    "--config-file",
    "--cache-dir",
    "--color",
    "--with",
    "--with-requirements",
    "--with-editable",
    "--index",
    "--index-url",
    "--extra-index-url",
    "--default-index",
    "--find-links",
    "-f",
    "--upgrade-package",
    "-P",
}
UV_REQUIRED = {
    "sync": {"--locked", "--no-dev", "--no-install-project"},
    "run": {"--locked", "--no-dev", "--no-sync"},
}
# --no-locked overrides --locked and --dev overrides --no-dev; both are accepted
# silently when they follow the required flag.
UV_FORBIDDEN = re.compile(
    r"--(with|no-project|index|extra-index|default-index|find-links|isolated|upgrade|frozen)"
    r"|--no-locked$|--dev$|-[UPf]$"
)
FORBIDDEN_ENVIRONMENT = re.compile(
    r"\bUV_(NO_SYNC|FROZEN|INDEX\w*|EXTRA_INDEX_URL|DEFAULT_INDEX|FIND_LINKS|ISOLATED)\b"
)
INSTALLER = re.compile(r"pip[0-9.]*|pipx|uvx|easy_install")
WRAPPERS = {"sudo", "env", "exec", "command", "nohup", "time"}
SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _shell_commands(workflow):
    # One logical command per entry: join shell continuations, drop comments.
    joined = re.sub(r"\\\n", " ", workflow)
    commands = []
    for line in joined.splitlines():
        command = " ".join(line.split()).removeprefix("- ").removeprefix("run: ")
        if command and not command.startswith("#"):
            commands.append(command)
    return commands


def _segments(command):
    """Split one shell line into simple commands without leading assignments."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # A quoted string that spans YAML lines, such as a jq program.
        tokens = command.split()
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token in SEPARATORS:
            while segment and ASSIGNMENT.match(segment[0]):
                segment.pop(0)
            if segment:
                yield segment
            segment = []
        else:
            segment.append(token)


def _uv_problem(tokens):
    """Check one uv invocation, whose global options may precede the subcommand."""
    options, subcommand, index = [], None, 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            if subcommand is not None:
                break  # The program that `uv run` starts, and its arguments.
            subcommand = token
        else:
            options.append(token.split("=", 1)[0])
            if "=" not in token and token in UV_VALUE_OPTIONS:
                index += 1
        index += 1
    if subcommand not in UV_REQUIRED:
        return f"uv subcommand {subcommand!r} is not allowed"
    forbidden = [option for option in options if UV_FORBIDDEN.match(option)]
    if forbidden:
        return f"uv {subcommand} uses {forbidden}"
    missing = UV_REQUIRED[subcommand] - set(options)
    if missing:
        return f"uv {subcommand} lacks {sorted(missing)}"
    return None


def unlocked_install_problems(workflow):
    """Return every way a trusted workflow could install or run unlocked code."""
    problems = [f"environment sets UV_{name}" for name in FORBIDDEN_ENVIRONMENT.findall(workflow)]
    for command in _shell_commands(workflow):
        for tokens in _segments(command):
            while len(tokens) > 1 and tokens[0] in WRAPPERS:
                tokens = tokens[1:]
            programs = [os.path.basename(token) for token in tokens]
            module = programs[2] if programs[1:2] == ["-m"] and len(programs) > 2 else ""
            if programs[0] == "uv":
                problem = _uv_problem(tokens)
                if problem:
                    problems.append(f"{problem}: {command}")
            if INSTALLER.fullmatch(programs[0]) or INSTALLER.fullmatch(module):
                problems.append(f"package installer: {command}")
            if programs[0] == "agentic-preflight":
                problems.append(f"installed console script: {command}")
            # The checker must be the program of this segment's own locked `uv run`.
            if "agentic_preflight" in tokens and programs[0] != "uv":
                problems.append(f"checker outside the locked environment: {command}")
    return problems


@pytest.mark.parametrize("filename", TRUSTED_WORKFLOWS)
def test_trusted_workflows_install_and_run_only_from_the_lock_file(filename):
    workflow = (ROOT / ".github/workflows" / filename).read_text()
    assert unlocked_install_problems(workflow) == []
    commands = _shell_commands(workflow)
    assert LOCKED_SYNC in commands
    checker = [command for command in commands if "-m agentic_preflight " in command]
    assert checker
    assert all(
        command.startswith(f"{LOCKED_RUN} python -m agentic_preflight ") for command in checker
    )
    flat = " ".join(workflow.split())
    assert flat.count("uses: astral-sh/setup-uv@") == len(PINNED_SETUP_UV.findall(flat)) == 1


@pytest.mark.parametrize(
    "line",
    [
        "run: uv tool install --python 3.11 .",
        "run: uv --python 3.11 tool install .",
        "run: uv -q tool install .",
        "run: uv build",
        "run: uv pip install .",
        "run: uv add click",
        "run: uv lock --upgrade",
        "run: uv sync --no-dev --no-install-project",
        "run: uv sync --locked --no-install-project",
        "run: uv sync --locked --no-dev",
        "run: uv --python 3.11 sync --no-dev --no-install-project",
        "run: uv sync --locked --no-dev --no-install-project --upgrade",
        "run: uv sync --locked --no-dev --no-install-project --upgrade-package click",
        "run: uv sync --locked --no-dev --no-install-project -U",
        "run: uv sync --locked --no-dev --no-install-project --index-url https://x.test",
        "run: uv sync --locked --no-dev --no-install-project --extra-index-url=https://x.test",
        "run: uv sync --locked --no-dev --no-install-project --index x=https://x.test",
        "run: uv sync --locked --no-dev --no-install-project --default-index https://x.test",
        "run: uv sync --locked --no-dev --no-install-project --frozen",
        "run: uv sync --locked --no-dev --no-install-project --no-locked",
        "run: uv sync --locked --no-dev --no-install-project --dev",
        "run: uv run --locked --no-dev --no-sync --no-locked python x.py",
        "run: uv -q run --no-dev --no-sync python -m agentic_preflight verify",
        "run: uv run --locked --no-sync python -m agentic_preflight verify",
        "run: uv run --locked --no-dev python -m agentic_preflight verify",
        "run: uv run --locked --no-dev --no-sync --with click python x.py",
        "run: uv run --locked --no-dev --no-sync --with-requirements r.txt python x.py",
        "run: uv run --locked --no-dev --no-sync --no-project python x.py",
        "run: uv run --locked --no-dev --no-sync --isolated python x.py",
        "run: cd sub && uv run --no-dev --no-sync python x.py",
        "run: sudo uv tool install .",
        "run: uvx pip-audit",
        "run: pip install .",
        "run: pip3.11 install .",
        "run: pipx install agentic-preflight",
        "run: python -m pip install .",
        "run: python3 -m pip3 install .",
        "run: sudo pip install .",
        "run: agentic-preflight verify HEAD",
        "run: python -m agentic_preflight verify HEAD",
        "run: uv run --locked --no-dev --no-sync true && python -m agentic_preflight verify HEAD",
        "UV_NO_SYNC: 1",
        "UV_FROZEN: true",
        "UV_INDEX: x=https://x.test",
        "UV_INDEX_URL: https://x.test",
        "UV_EXTRA_INDEX_URL: https://x.test",
        "run: UV_FROZEN=1 uv run --locked --no-dev --no-sync python x.py",
    ],
)
def test_unlocked_install_variants_are_rejected(line):
    assert unlocked_install_problems(f"    steps:\n      - {line}\n") != []


def test_locked_install_variants_are_accepted():
    workflow = (
        f"      - run: {LOCKED_SYNC}\n"
        "      - run: uv --quiet run --locked --no-dev --no-sync --python=3.11 python x.py\n"
        f"      - run: |\n          {LOCKED_RUN} \\\n            python -m agentic_preflight verify\n"
    )
    assert unlocked_install_problems(workflow) == []


def test_build_backend_is_pinned():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requires = project["build-system"]["requires"]
    assert requires
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", item) for item in requires)


def test_approval_events_outputs_and_dependencies():
    workflow = (ROOT / ".github/workflows/human-approval.yml").read_text()
    events = " ".join(workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0].split())
    assert events == (
        "pull_request_target: types: [opened, reopened, synchronize, ready_for_review, "
        "auto_merge_enabled, auto_merge_disabled]"
    )
    policy = workflow.split("\n  policy:\n", 1)[1].split("\n  environment:\n", 1)[0]
    outputs = policy.split("    outputs:\n", 1)[1].split("    steps:\n", 1)[0]
    assert [line.strip() for line in outputs.splitlines() if line.strip()] == [
        f"{key}: ${{{{ steps.policy.outputs.{key} }}}}"
        for key in (
            "approved",
            "approved_head",
            "approval_mode",
            "environment",
            "requires_approval",
        )
    ] + ["dependabot_exempt: ${{ steps.dependabot.outputs.verified }}"]
    environment = " ".join(
        workflow.split("\n  environment:\n", 1)[1].split("\n  approval:\n", 1)[0].split()
    )
    assert (
        "needs: policy if: >- needs.policy.outputs.requires_approval == 'true' && "
        "needs.policy.outputs.dependabot_exempt != 'true' && "
        "needs.policy.outputs.approval_mode == 'environment'" in environment
    )
    assert "environment: name: ${{ needs.policy.outputs.environment }}" in environment
    approval = " ".join(workflow.split("\n  approval:\n", 1)[1].split())
    assert "needs: [dependabot-policy, policy, environment] if: always()" in approval
    for variable, source in {
        "APPROVED": "policy.outputs.approved",
        "APPROVAL_MODE": "policy.outputs.approval_mode",
        "ENVIRONMENT_RESULT": "environment.result",
        "POLICY_RESULT": "policy.result",
        "REQUIRES_APPROVAL": "policy.outputs.requires_approval",
        "DEPENDABOT_EXEMPT": "policy.outputs.dependabot_exempt",
    }.items():
        assert f"{variable}: ${{{{ needs.{source} }}}}" in approval


@pytest.fixture
def run_step(tmp_path):
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        pytest.skip("Bash and jq are required to execute the Linux workflow steps")

    def run(
        filename,
        *,
        code=0,
        fork=False,
        mode="manual_merge",
        auto_merge=False,
        environment=True,
        fetch_code=0,
    ):
        payload = {
            "ok": code == 0,
            "data": {
                "reason": "missing_note" if code else None,
                "approved": mode == "manual_merge",
                "approval_mode": mode,
                "approval_environment": "high-risk-review",
                "requires_human_approval": True,
            },
        }
        (tmp_path / "response.json").write_text(json.dumps(payload))
        (tmp_path / "environment-response.json").write_text(
            json.dumps(
                [
                    {
                        "environments": [
                            {
                                "name": "high-risk-review",
                                "protection_rules": [
                                    {
                                        "type": "required_reviewers",
                                        "reviewers": [{}] if environment else [],
                                    }
                                ],
                            }
                        ]
                    }
                ]
            )
        )
        (tmp_path / "output.txt").write_text("")
        # Seed stale success to prove the command redirection replaces it.
        (tmp_path / "approval-policy.json").write_text('{"ok":true,"data":{"approved":true}}')
        (tmp_path / "attestation-result.json").write_text('{"ok":true}')
        harness = r"""
uv() {
  printf '%s\n' "$*" >> uv-args.txt
  [ "$1 $2 $3 $4 $5 $6" = "run --locked --no-dev --no-sync --python 3.11" ] || return 97
  shift 6
  [ "$1 $2 $3" = "python -m agentic_preflight" ] || return 98
  shift 3
  agentic-preflight "$@"
}
agentic-preflight() {
  printf '%s\n' "$@" > helper-args.txt
  printf 'call\n' >> helper-calls.txt
  cat response.json
  return "$TEST_EXIT"
}
git() {
  printf '%s\n' "$@" >> git-args.txt
  if [ "$1" = "fetch" ]; then return "$TEST_FETCH_EXIT"; fi
}
gh() {
  printf '%s\n' "$@" >> gh-args.txt
  case "$*" in
    *reviews*) echo '[]' ;;
    *environments*) cat environment-response.json ;;
    *) echo "$TEST_AUTO_MERGE" ;;
  esac
}
RUNNER_TEMP=.
GITHUB_OUTPUT=output.txt
"""
        env = {
            **os.environ,
            "HEAD_SHA": HEAD,
            "ATTESTED_SHA": HEAD,
            "BASE_SHA": BASE,
            "HEAD_REF": BRANCH,
            "GITHUB_REPOSITORY": "owner/project",
            "HEAD_REPOSITORY": "contributor/project" if fork else "owner/project",
            "HEAD_REPOSITORY_URL": "https://github.com/contributor/project.git",
            "PR_AUTHOR": "author",
            "PR_NUMBER": "88",
            "TEST_EXIT": str(code),
            "TEST_FETCH_EXIT": str(fetch_code),
            "TEST_AUTO_MERGE": str(auto_merge).lower(),
        }
        result = subprocess.run(
            [
                bash,
                "--noprofile",
                "--norc",
                "-e",
                "-u",
                "-o",
                "pipefail",
                "-c",
                harness + _policy_script(filename),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result, tmp_path

    return run


@pytest.mark.parametrize("filename", ["attestation.yml", "human-approval.yml"])
@pytest.mark.parametrize("code", [2, 4])
def test_failures_print_json_keep_exit_and_never_publish_outputs(run_step, filename, code):
    result, path = run_step(filename, code=code)
    assert result.returncode == code, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["data"]["reason"] == "missing_note"
    assert (path / "output.txt").read_text() == ""
    assert (path / "helper-calls.txt").read_text().splitlines() == ["call"]


@pytest.mark.parametrize("filename", ["attestation.yml", "human-approval.yml"])
@pytest.mark.parametrize("fork", [True, False])
def test_success_passes_fixed_event_and_resolved_fork_remote(run_step, filename, fork):
    result, path = run_step(filename, fork=fork)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
    args = (path / "helper-args.txt").read_text().splitlines()
    assert args[:2] == ["verify" if filename == "attestation.yml" else "approval-check", HEAD]
    uv_calls = (path / "uv-args.txt").read_text().splitlines()
    assert len(uv_calls) == 1
    assert f"uv {uv_calls[0]}".startswith(
        f"{LOCKED_RUN} python -m agentic_preflight {args[0]} {HEAD}"
    )
    assert not (path / "injected").exists()
    git_args = (path / "git-args.txt").read_text().splitlines()
    if fork:
        assert git_args[:4] == [
            "remote",
            "add",
            "preflight-contributor",
            "https://github.com/contributor/project.git",
        ]
    assert git_args[4 if fork else 0 :] == [
        "fetch",
        "--no-tags",
        "--",
        "preflight-contributor" if fork else "origin",
        HEAD,
        "+refs/notes/agentic-preflight:refs/notes/agentic-preflight",
        "+refs/agentic-preflight/evidence/*:refs/agentic-preflight/evidence/*",
    ]
    if filename == "human-approval.yml":
        assert args[args.index("--base") + 1] == BASE
        assert "--report-only" in args
        assert (path / "output.txt").read_text().splitlines() == [
            "approved=true",
            "approved_head=",
            "approval_mode=manual_merge",
            "environment=high-risk-review",
            "requires_approval=true",
        ]
    else:
        assert "--mode" not in args


@pytest.mark.parametrize("filename", ["attestation.yml", "human-approval.yml"])
@pytest.mark.parametrize("fork", [True, False])
def test_fetch_failure_stops_before_verification_or_approval(run_step, filename, fork):
    result, path = run_step(filename, fork=fork, fetch_code=128)
    assert result.returncode == 128
    assert not (path / "helper-calls.txt").exists()
    assert not (path / "gh-args.txt").exists()
    assert (path / "output.txt").read_text() == ""


def test_auto_merge_remains_blocked(run_step):
    result, path = run_step("human-approval.yml", auto_merge=True)
    assert result.returncode == 1
    assert "Disable auto-merge" in result.stderr
    assert (path / "output.txt").read_text() == ""


@pytest.mark.parametrize("environment", [True, False])
def test_environment_gate_configuration_remains_required(run_step, environment):
    result, path = run_step("human-approval.yml", mode="environment", environment=environment)
    assert result.returncode == (0 if environment else 1), result.stderr
    output = (path / "output.txt").read_text()
    if environment:
        assert "approved=false" in output
        assert "approval_mode=environment" in output
    else:
        assert output == ""
        assert "must exist with a required reviewer" in result.stderr


def test_failed_policy_cannot_be_green_from_stale_approval_outputs(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is unavailable")
    workflow = (ROOT / ".github/workflows/human-approval.yml").read_text()
    script = textwrap.dedent(
        workflow.split("      - name: Enforce the configured approval result", 1)[1].split(
            "        run: |\n", 1
        )[1]
    )
    result = subprocess.run(
        [bash, "-e", "-u", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "POLICY_RESULT": "failure",
            "APPROVED": "true",
            "REQUIRES_APPROVAL": "false",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "No high-risk approval is required" not in result.stdout
