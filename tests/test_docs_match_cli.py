"""The docs must not lie.

SKILL.md and its reference files were written last, against the finished CLI. These
tests keep them that way: a command renamed in code and not in the skill is caught
here rather than by an agent at runtime, halfway through someone's release.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentic_preflight.cli import main
from agentic_preflight.config import Config
from agentic_preflight.envelope import ExitCode

SKILL_DIR = Path(__file__).parent.parent / "skill"
SKILL = SKILL_DIR / "SKILL.md"
REFERENCE = SKILL_DIR / "reference"
README = Path(__file__).parent.parent / "README.md"
CONFIGURATION = Path(__file__).parent.parent / "docs" / "configuration.md"


def real_commands() -> set[str]:
    names = set(main.commands)
    for name, cmd in main.commands.items():
        for sub in getattr(cmd, "commands", {}):
            names.add(f"{name} {sub}")
    return names


def code_regions(text: str) -> str:
    """Only fenced blocks and inline code spans.

    Scanning raw prose would treat "agentic-preflight refuses to push" as an
    invocation of a `refuses` command. Commands appear in code, so only code
    is searched.
    """
    fenced = re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL)
    inline = re.findall(r"`([^`\n]+)`", text)
    return "\n".join(fenced + inline)


def documented_commands(text: str) -> set[str]:
    """Every `agentic-preflight <command>` invocation appearing in a doc's code."""
    found = set()
    for match in re.finditer(
        r"(?<![$/])agentic-preflight ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?",
        code_regions(text),
    ):
        name, sub = match.group(1), match.group(2)
        found.add(f"{name} {sub}" if sub else name)
        if sub:
            # `stage run lint` documents the `stage` group too.
            found.add(name)
    return found


# -- commands -------------------------------------------------------------


def test_skill_md_exists_with_front_matter():
    text = SKILL.read_text()
    assert text.startswith("---")
    assert "name: agentic-preflight" in text
    assert "description:" in text


@pytest.mark.parametrize(
    "doc",
    [
        SKILL,
        REFERENCE / "commands.md",
        REFERENCE / "findings-schema.md",
        REFERENCE / "docs-rubric.md",
        README,
    ],
)
def test_every_documented_command_exists(doc):
    real = real_commands()
    documented = documented_commands(doc.read_text())
    unknown = {c for c in documented if c not in real and c.split()[0] not in real}
    assert unknown == set(), f"{doc.name} documents commands that do not exist: {unknown}"


def test_every_real_command_is_documented_somewhere():
    documented = set()
    for doc in (SKILL, REFERENCE / "commands.md"):
        documented |= documented_commands(doc.read_text())
    missing = real_commands() - documented
    assert missing == set(), f"undocumented commands: {missing}"


# -- exit codes -----------------------------------------------------------


def test_documented_exit_codes_match_the_enum():
    text = (REFERENCE / "commands.md").read_text()
    for code in ExitCode:
        assert str(int(code)) in text, f"exit code {code.name}={int(code)} is undocumented"


def test_skill_documents_the_universal_recovery_rule():
    text = SKILL.read_text()
    assert "exit 3" in text
    assert "status" in text


def test_skill_links_the_complete_command_reference():
    text = SKILL.read_text()
    assert "reference/commands.md" in text


def test_skill_documents_mergeback_conflict_retry():
    text = SKILL.read_text()
    assert "`mergeback` is the legal retry" in text
    assert "report is stored in the event log" in text
    assert "no outbound transition" not in text


# -- config ---------------------------------------------------------------


def test_configuration_example_uses_only_real_sections():
    text = CONFIGURATION.read_text()
    documented = set(re.findall(r"^\[([a-z]+)\]$", text, re.MULTILINE))
    real = set(Config.model_fields)
    unknown = documented - real
    assert unknown == set(), f"README documents config sections that do not exist: {unknown}"


def test_every_config_section_is_documented_in_the_configuration_reference():
    text = CONFIGURATION.read_text()
    documented = set(re.findall(r"^\[([a-z]+)\]$", text, re.MULTILINE))
    missing = set(Config.model_fields) - documented
    assert missing == set(), f"undocumented config sections: {missing}"


def test_the_configuration_example_actually_parses(tmp_path):
    """The example must be valid config, not plausible-looking config."""
    import tomllib

    text = CONFIGURATION.read_text()
    block = re.search(r"```toml\n(.*?)```", text, re.DOTALL).group(1)
    parsed = tomllib.loads(block)
    Config.model_validate(parsed)


# -- honesty --------------------------------------------------------------


def test_the_readme_states_the_gate_is_not_a_security_boundary():
    """The design requires this be said plainly; do not let it be edited away."""
    text = README.read_text().lower()
    assert "not a security boundary" in text
    assert "--no-verify" in text
    assert "manual" in text


def test_the_skill_documents_the_findings_id_prohibition():
    for doc in (SKILL, REFERENCE / "findings-schema.md"):
        text = doc.read_text()
        assert "`id`" in text or '"id"' in text
        assert "stage" in text


def test_the_skill_documents_both_pr_modes_and_their_approval_boundary():
    text = SKILL.read_text()
    assert '[pr] mode = "auto"' in text
    assert '[pr] mode = "manual"' in text
    assert "standing authorization" in text
    assert "never open the PR for them" in text


def test_explicit_publication_request_does_not_require_double_confirmation():
    text = SKILL.read_text()
    assert "create/open a pull request authorizes the matching push" in text
    assert "Do not ask them to confirm the same publication twice" in text
    assert 'generic "proceed"' in text


def test_the_skill_documents_all_high_risk_approval_modes():
    text = SKILL.read_text()
    assert "`manual_merge`" in text
    assert "`environment`" in text
    assert "`peer_review`" in text
    assert "never merge or enable auto-merge" in text

    configuration = (README.parent / "docs" / "configuration.md").read_text()
    assert '`mode = "manual_merge"` is the default' in configuration


def test_an_explicit_cleanup_request_needs_no_second_approval():
    text = SKILL.read_text()
    assert "cleanup in the same turn without asking again" in text
    assert "delete the local PR source branch and the remote PR source branch" in text


def test_the_skill_documents_the_project_uninstall_trigger_and_scope():
    text = SKILL.read_text()
    assert "agentic-preflight:uninstall" in text
    assert "without another confirmation" in text
    assert ".agentic-preflight.toml" in text
    assert "Do not remove other hook behavior" in text
    assert "refs/notes/agentic-preflight" in text


def test_the_docs_rubric_leads_with_the_obligation_test():
    text = (REFERENCE / "docs-rubric.md").read_text()
    assert "Would a reader following the current documentation now be wrong?" in text
    assert "Zero findings is a normal" in text


# -- the help text itself -------------------------------------------------


def test_help_lists_every_command():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for name in main.commands:
        assert name in result.output
