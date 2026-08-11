"""Cross-agent installation of the bundled Agent Skill."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentic_preflight import integrations
from agentic_preflight.cli import main
from agentic_preflight.envelope import ExitCode


@pytest.fixture
def source_skill(tmp_path):
    source = tmp_path / "source-skill"
    (source / "reference").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: agentic-preflight\ndescription: Test skill.\n---\n\nFollow the workflow.\n"
    )
    (source / "reference" / "commands.md").write_text("# Commands\n")
    return source


def test_resolves_codex_and_claude_user_locations(tmp_path):
    targets = integrations.resolve_targets(["codex", "claude"], scope="user", home=tmp_path)
    assert targets == [
        integrations.InstallTarget("codex", tmp_path / ".agents" / "skills" / "agentic-preflight"),
        integrations.InstallTarget("claude", tmp_path / ".claude" / "skills" / "agentic-preflight"),
    ]


def test_resolves_cursor_opencode_and_amp_documented_locations(tmp_path):
    targets = integrations.resolve_targets(
        ["cursor", "opencode", "amp"], scope="user", home=tmp_path
    )
    assert targets == [
        integrations.InstallTarget(
            "cursor", tmp_path / ".cursor" / "skills" / "agentic-preflight"
        ),
        integrations.InstallTarget(
            "opencode",
            tmp_path / ".config" / "opencode" / "skills" / "agentic-preflight",
        ),
        integrations.InstallTarget(
            "amp", tmp_path / ".config" / "agents" / "skills" / "agentic-preflight"
        ),
    ]


def test_resolves_new_project_locations_and_preserves_shared_agent_targets(tmp_path):
    targets = integrations.resolve_targets(
        ["codex", "cursor", "opencode", "amp"],
        scope="project",
        project_root=tmp_path,
    )
    assert targets == [
        integrations.InstallTarget(
            "codex", tmp_path / ".agents" / "skills" / "agentic-preflight"
        ),
        integrations.InstallTarget(
            "cursor", tmp_path / ".cursor" / "skills" / "agentic-preflight"
        ),
        integrations.InstallTarget(
            "opencode", tmp_path / ".opencode" / "skills" / "agentic-preflight"
        ),
        integrations.InstallTarget(
            "amp", tmp_path / ".agents" / "skills" / "agentic-preflight"
        ),
    ]


def test_resolves_project_locations_at_the_repository_root(tmp_path):
    targets = integrations.resolve_targets(
        ["codex", "claude"], scope="project", project_root=tmp_path
    )
    assert [target.path for target in targets] == [
        tmp_path / ".agents" / "skills" / "agentic-preflight",
        tmp_path / ".claude" / "skills" / "agentic-preflight",
    ]


def test_custom_target_is_a_skills_root(tmp_path):
    custom_root = tmp_path / "other-agent" / "skills"
    targets = integrations.resolve_targets(
        [], scope="user", custom_roots=[custom_root], home=tmp_path
    )
    assert targets == [integrations.InstallTarget("custom", custom_root / "agentic-preflight")]


def test_install_copies_the_whole_skill_and_records_ownership(tmp_path, source_skill):
    results = integrations.install_integrations(
        ["codex", "claude"],
        home=tmp_path,
        source_dir=source_skill,
        source_version="1.2.3",
    )

    assert [result["action"] for result in results] == ["installed", "installed"]
    assert all(result["status"] == "current" for result in results)
    for destination in (
        tmp_path / ".agents" / "skills" / "agentic-preflight",
        tmp_path / ".claude" / "skills" / "agentic-preflight",
    ):
        assert (destination / "SKILL.md").read_text() == (source_skill / "SKILL.md").read_text()
        assert (destination / "reference" / "commands.md").is_file()
        metadata = json.loads((destination / integrations.INSTALL_METADATA).read_text())
        assert metadata["package_version"] == "1.2.3"


def test_reinstall_of_a_current_copy_is_idempotent(tmp_path, source_skill):
    integrations.install_integrations(
        ["codex"], home=tmp_path, source_dir=source_skill, source_version="1.0"
    )
    results = integrations.install_integrations(
        ["codex"], home=tmp_path, source_dir=source_skill, source_version="1.0"
    )
    assert results[0]["action"] == "unchanged"


def test_update_replaces_an_unmodified_outdated_copy(tmp_path, source_skill):
    integrations.install_integrations(
        ["codex"], home=tmp_path, source_dir=source_skill, source_version="1.0"
    )
    (source_skill / "SKILL.md").write_text(
        "---\nname: agentic-preflight\ndescription: Updated.\n---\n\nNew workflow.\n"
    )

    before = integrations.integration_status(
        ["codex"], home=tmp_path, source_dir=source_skill, source_version="2.0"
    )
    assert before[0]["status"] == "outdated"

    results = integrations.install_integrations(
        ["codex"],
        home=tmp_path,
        source_dir=source_skill,
        source_version="2.0",
        update_only=True,
    )
    assert results[0]["action"] == "updated"
    assert results[0]["installed_version"] == "2.0"


def test_update_skips_an_integration_that_is_not_installed(tmp_path, source_skill):
    results = integrations.install_integrations(
        ["codex"],
        home=tmp_path,
        source_dir=source_skill,
        source_version="1.0",
        update_only=True,
    )
    assert results[0]["action"] == "skipped_missing"
    assert results[0]["status"] == "missing"


def test_lifecycle_operation_table_covers_every_inspection_status():
    statuses = {"missing", "unmanaged", "modified", "outdated", "current"}
    assert set(integrations.OPERATION_SPECS) == {
        integrations.IntegrationOperation.INSTALL,
        integrations.IntegrationOperation.UPDATE,
        integrations.IntegrationOperation.UNINSTALL,
    }
    assert all(set(spec.actions) == statuses for spec in integrations.OPERATION_SPECS.values())


def test_modified_copy_is_preserved_unless_force_is_explicit(tmp_path, source_skill):
    integrations.install_integrations(
        ["codex"], home=tmp_path, source_dir=source_skill, source_version="1.0"
    )
    destination = tmp_path / ".agents" / "skills" / "agentic-preflight"
    (destination / "SKILL.md").write_text("my local workflow\n")

    status = integrations.integration_status(
        ["codex"], home=tmp_path, source_dir=source_skill, source_version="1.0"
    )
    assert status[0]["status"] == "modified"
    with pytest.raises(integrations.IntegrationConflict):
        integrations.install_integrations(
            ["codex"], home=tmp_path, source_dir=source_skill, source_version="1.0"
        )
    assert (destination / "SKILL.md").read_text() == "my local workflow\n"

    results = integrations.install_integrations(
        ["codex"],
        home=tmp_path,
        source_dir=source_skill,
        source_version="1.0",
        force=True,
    )
    assert results[0]["action"] == "replaced"
    assert (destination / "SKILL.md").read_text() == (source_skill / "SKILL.md").read_text()


def test_conflicts_are_preflighted_before_any_destination_changes(tmp_path, source_skill):
    codex = tmp_path / ".agents" / "skills" / "agentic-preflight"
    codex.mkdir(parents=True)
    (codex / "SKILL.md").write_text("unmanaged\n")

    with pytest.raises(integrations.IntegrationConflict):
        integrations.install_integrations(
            ["codex", "claude"],
            home=tmp_path,
            source_dir=source_skill,
            source_version="1.0",
        )
    assert not (tmp_path / ".claude" / "skills" / "agentic-preflight").exists()


def test_uninstall_removes_managed_copy_but_preserves_modified_copy(tmp_path, source_skill):
    integrations.install_integrations(
        ["codex"], home=tmp_path, source_dir=source_skill, source_version="1.0"
    )
    destination = tmp_path / ".agents" / "skills" / "agentic-preflight"
    (destination / "SKILL.md").write_text("my local workflow\n")

    with pytest.raises(integrations.IntegrationConflict):
        integrations.uninstall_integrations(
            ["codex"], home=tmp_path, source_dir=source_skill, source_version="1.0"
        )
    assert destination.exists()

    results = integrations.uninstall_integrations(
        ["codex"],
        home=tmp_path,
        source_dir=source_skill,
        source_version="1.0",
        force=True,
    )
    assert results[0]["action"] == "removed"
    assert results[0]["status"] == "missing"
    assert not destination.exists()


def test_cli_install_and_status_keep_the_single_json_contract(tmp_path):
    home = tmp_path / "home"
    runner = CliRunner()

    installed = runner.invoke(
        main,
        ["integrations", "install", "codex", "claude"],
        env={"HOME": str(home)},
        catch_exceptions=False,
    )
    assert installed.exit_code == 0
    assert len(installed.stdout.strip().splitlines()) == 1
    payload = json.loads(installed.stdout)
    assert [item["action"] for item in payload["data"]["integrations"]] == [
        "installed",
        "installed",
    ]
    assert "codex: installed" in installed.stderr
    assert "claude: installed" in installed.stderr

    status = runner.invoke(
        main,
        ["integrations", "status"],
        env={"HOME": str(home)},
        catch_exceptions=False,
    )
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    by_integration = {
        item["integration"]: item for item in payload["data"]["integrations"]
    }
    assert set(by_integration) == set(integrations.SUPPORTED_INTEGRATIONS)
    assert by_integration["codex"]["status"] == "current"
    assert by_integration["claude"]["status"] == "current"
    assert all(
        by_integration[name]["status"] == "missing"
        for name in {"cursor", "opencode", "amp"}
    )


def test_cli_refuses_an_unmanaged_copy_with_a_structured_error(tmp_path):
    home = tmp_path / "home"
    destination = home / ".agents" / "skills" / "agentic-preflight"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("unmanaged\n")

    result = CliRunner().invoke(
        main,
        ["integrations", "install", "codex"],
        env={"HOME": str(home)},
        catch_exceptions=False,
    )
    assert result.exit_code == ExitCode.PRECONDITION
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "integration_conflict"
    assert payload["data"]["conflicts"][0]["status"] == "unmanaged"


def test_cli_custom_target_does_not_implicitly_select_known_agents(tmp_path):
    home = tmp_path / "home"
    custom_root = tmp_path / "other-agent" / "skills"
    runner = CliRunner()

    installed = runner.invoke(
        main,
        ["integrations", "install", "--target", str(custom_root)],
        env={"HOME": str(home)},
        catch_exceptions=False,
    )
    assert installed.exit_code == 0
    payload = json.loads(installed.stdout)
    assert [item["integration"] for item in payload["data"]["integrations"]] == ["custom"]
    assert not (home / ".agents" / "skills" / "agentic-preflight").exists()
    assert not (home / ".claude" / "skills" / "agentic-preflight").exists()

    status = runner.invoke(
        main,
        ["integrations", "status", "--target", str(custom_root)],
        env={"HOME": str(home)},
        catch_exceptions=False,
    )
    payload = json.loads(status.stdout)
    assert len(payload["data"]["integrations"]) == 1
    assert payload["data"]["integrations"][0]["status"] == "current"


def test_wheel_force_includes_the_canonical_skill_directory():
    root = Path(__file__).parent.parent
    config = tomllib.loads((root / "pyproject.toml").read_text())
    force_include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include["skill"] == "agentic_preflight/_bundled_skill"
    assert integrations.bundled_skill_dir() == root / "skill"


@pytest.mark.parametrize("command", ["install", "status", "update", "uninstall"])
def test_integration_commands_share_the_same_targeting_options(command):
    result = CliRunner().invoke(main, ["integrations", command, "--help"])
    assert result.exit_code == 0
    assert "--scope [user|project]" in result.output
    assert "--target DIRECTORY" in result.output


@pytest.mark.parametrize("command", ["install", "update", "uninstall"])
def test_mutating_integration_commands_keep_force_option(command):
    result = CliRunner().invoke(main, ["integrations", command, "--help"])
    assert result.exit_code == 0
    assert "--force" in result.output
