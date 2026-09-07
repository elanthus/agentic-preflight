import json

import pytest
from pydantic import ValidationError

from agentic_preflight.config import Config, ConfigError, _describe, load_config


def test_defaults_apply_when_no_config_file_exists(tmp_repo, tmp_path):
    cfg = load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert cfg.general.base_ref == "main"
    assert cfg.review.blocking_severities == ["critical", "high"]
    assert cfg.review.executor == "in_harness"
    assert cfg.review.command is None
    assert cfg.review.require_command_for == []
    assert cfg.docs.enabled is True
    assert cfg.context.enabled is True
    assert cfg.context.max_bytes == 24_000
    assert cfg.context.entry_max_bytes == 4_000
    assert cfg.context.extra_paths == []
    assert cfg.worktree.copy_files == [".env"]
    assert cfg.worktree.root is None
    assert cfg.worktree.mode == "in_place"
    assert cfg.worktree.setup_command is None
    assert cfg.gate.mode == "token"
    assert cfg.pr.mode == "auto"
    assert cfg.pr.automated_cleanup is True
    assert cfg.approval.mode == "manual_merge"
    assert cfg.approval.environment == "high-risk-review"
    assert cfg.policy.human_review_paths == []
    assert cfg.policy.high_risk_paths == []
    assert cfg.policy.medium_risk_paths == []
    assert cfg.diff.max_bytes == 200_000
    assert "*.lock" in cfg.diff.exclude


def test_repo_config_is_read(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text(
        "[general]\nbase_ref = 'develop'\n\n[commands]\ntest = 'pytest -q'\n"
    )
    cfg = load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert cfg.general.base_ref == "develop"
    assert cfg.commands.test == "pytest -q"


def test_repo_config_wins_over_user_config(tmp_repo, tmp_path):
    user_dir = tmp_path / "userconf"
    user_dir.mkdir()
    (user_dir / "config.toml").write_text("[general]\nbase_ref = 'from-user'\n")
    (tmp_repo / ".agentic-preflight.toml").write_text("[general]\nbase_ref = 'from-repo'\n")

    cfg = load_config(tmp_repo, user_config_dir=user_dir)
    assert cfg.general.base_ref == "from-repo"


def test_user_config_fills_sections_the_repo_omits(tmp_repo, tmp_path):
    user_dir = tmp_path / "userconf"
    user_dir.mkdir()
    (user_dir / "config.toml").write_text("[stage]\ntimeout_seconds = 900\n")
    (tmp_repo / ".agentic-preflight.toml").write_text("[general]\nbase_ref = 'develop'\n")

    cfg = load_config(tmp_repo, user_config_dir=user_dir)
    assert cfg.general.base_ref == "develop"
    assert cfg.stage.timeout_seconds == 900


def test_an_unknown_key_is_an_error_naming_the_key(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text("[general]\nbase_reff = 'main'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "base_reff" in str(exc.value)


def test_an_unknown_section_is_an_error_naming_the_section(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text("[nonsense]\nx = 1\n")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "nonsense" in str(exc.value)


def test_malformed_toml_is_a_config_error_not_a_traceback(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text("[general\nbase_ref = 'main'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert ".agentic-preflight.toml" in str(exc.value)


def test_blocking_severities_reject_a_bogus_severity(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text(
        "[review]\nblocking_severities = ['critical', 'spicy']\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "spicy" in str(exc.value)


def test_review_executor_and_required_risk_levels_are_validated(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text(
        "[review]\nexecutor = 'command'\ncommand = 'reviewer --json'\n"
        "require_command_for = ['medium', 'high']\n"
    )
    cfg = load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert cfg.review.executor == "command"
    assert cfg.review.command == "reviewer --json"
    assert cfg.review.require_command_for == ["medium", "high"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("executor = 'telepathy'", "executor"),
        ("require_command_for = ['extreme']", "require_command_for"),
    ],
)
def test_review_executor_rejects_unknown_values(tmp_repo, tmp_path, body, message):
    (tmp_repo / ".agentic-preflight.toml").write_text(f"[review]\n{body}\n")
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")


def test_gate_mode_rejects_an_unknown_mode(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text("[gate]\nmode = 'yolo'\n")
    with pytest.raises(ConfigError):
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")


def test_pr_mode_rejects_an_unknown_mode(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text("[pr]\nmode = 'sometimes'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "[pr] mode" in str(exc.value)


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_pr_modes_are_explicit_configuration_options(tmp_repo, tmp_path, mode):
    (tmp_repo / ".agentic-preflight.toml").write_text(f"[pr]\nmode = {mode!r}\n")
    cfg = load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert cfg.pr.mode == mode


def test_automatic_cleanup_can_be_disabled_with_the_public_camel_case_key(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text(
        "[pr]\nmode = 'auto'\nautomatedCleanup = false\n"
    )
    cfg = load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert cfg.pr.automated_cleanup is False


def test_approval_mode_rejects_an_unknown_mode(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text("[approval]\nmode = 'hope'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "[approval] mode" in str(exc.value)


@pytest.mark.parametrize("mode", ["manual_merge", "environment", "peer_review"])
def test_approval_modes_are_explicit_configuration_options(tmp_repo, tmp_path, mode):
    (tmp_repo / ".agentic-preflight.toml").write_text(
        f"[approval]\nmode = {mode!r}\nenvironment = 'production-review'\n"
    )
    cfg = load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert cfg.approval.mode == mode
    assert cfg.approval.environment == "production-review"


def test_approval_environment_must_not_be_empty(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text(
        "[approval]\nmode = 'environment'\nenvironment = '   '\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "environment must not be empty" in str(exc.value)


@pytest.mark.parametrize("pattern", ["", "/absolute/**", "src/../secrets/**"])
def test_policy_rejects_unsafe_patterns(tmp_repo, tmp_path, pattern):
    (tmp_repo / ".agentic-preflight.toml").write_text(
        f"[policy]\nhuman_review_paths = [{pattern!r}]\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "human_review_paths" in str(exc.value)


@pytest.mark.parametrize("pattern", ["", "/absolute.md", "rules/../secrets.md"])
def test_context_rejects_unsafe_extra_paths(tmp_repo, tmp_path, pattern):
    (tmp_repo / ".agentic-preflight.toml").write_text(f"[context]\nextra_paths = [{pattern!r}]\n")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "[context] extra_paths" in str(exc.value)


def test_worktree_mode_rejects_an_unknown_value(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text("[worktree]\nmode = 'careless'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "careless" in str(exc.value)


@pytest.mark.parametrize("mode", ["in_place", "reusable", "strict"])
def test_all_worktree_modes_are_explicit_configuration_options(tmp_repo, tmp_path, mode):
    (tmp_repo / ".agentic-preflight.toml").write_text(f"[worktree]\nmode = {mode!r}\n")
    cfg = load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert cfg.worktree.mode == mode


def test_config_is_constructible_with_no_arguments():
    """Defaults must stand alone so tests and `init` can build one cheaply."""
    assert Config().general.base_ref == "main"


@pytest.mark.parametrize(
    ("section", "values", "key"),
    [
        ("gate", {"mode": "yolo"}, "mode"),
        ("pr", {"mode": "sometimes"}, "mode"),
        ("approval", {"mode": "hope"}, "mode"),
        ("worktree", {"mode": "careless"}, "mode"),
        ("review", {"executor": "telepathy"}, "executor"),
        ("review", {"require_command_for": ["extreme"]}, "require_command_for"),
        ("review", {"blocking_severities": ["spicy"]}, "blocking_severities"),
        ("docs", {"blocking_severities": ["spicy"]}, "blocking_severities"),
        ("approval", {"mode": "environment", "environment": "  "}, "environment"),
        ("policy", {"human_review_paths": [""]}, "human_review_paths"),
        ("policy", {"high_risk_paths": ["/absolute"]}, "high_risk_paths"),
        ("policy", {"medium_risk_paths": ["src/../private"]}, "medium_risk_paths"),
        ("context", {"extra_paths": ["../secret"]}, "extra_paths"),
    ],
)
@pytest.mark.parametrize("source", ["repo", "user"])
def test_invalid_values_fail_for_construction_snapshots_and_toml(
    tmp_repo, tmp_path, section, values, key, source
):
    snapshot = {section: values}
    for construct in (
        lambda: Config(**snapshot),
        lambda: Config.model_validate(snapshot),
        lambda: Config.model_validate_json(json.dumps(snapshot)),
    ):
        with pytest.raises(ValidationError) as error:
            construct()
        assert section in str(error.value)
        assert key in str(error.value)

    user_dir = tmp_path / "userconf"
    user_dir.mkdir()
    path = tmp_repo / ".agentic-preflight.toml" if source == "repo" else user_dir / "config.toml"
    path.write_text(
        f"[{section}]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in values.items())
    )
    with pytest.raises(ConfigError) as error:
        load_config(tmp_repo, user_config_dir=user_dir)
    assert str(path) in str(error.value)
    assert section in str(error.value)
    assert key in str(error.value)


def test_section_replacement_discards_invalid_overridden_values(tmp_repo, tmp_path):
    user_dir = tmp_path / "userconf"
    user_dir.mkdir()
    (user_dir / "config.toml").write_text("[review]\nexecutor = 'invalid'\ncommand = 'old'\n")
    (tmp_repo / ".agentic-preflight.toml").write_text("[review]\nmax_findings = 10\n")
    cfg = load_config(tmp_repo, user_config_dir=user_dir)
    assert cfg.review.executor == "in_harness"
    assert cfg.review.command is None
    assert cfg.review.max_findings == 10


def test_valid_patterns_aliases_and_conditional_environment_round_trip():
    snapshot = {
        "policy": {"human_review_paths": ["src/**", "docs/*.md"]},
        "context": {"extra_paths": ["rules/**"]},
        "approval": {"mode": "manual_merge", "environment": ""},
        "pr": {"automatedCleanup": False},
    }
    cfg = Config.model_validate(snapshot)
    assert cfg.approval.environment == ""
    assert cfg.pr.automated_cleanup is False
    serialized = cfg.model_dump(mode="json")
    assert Config.model_validate(serialized).model_dump(mode="json") == serialized
    assert cfg.model_dump(mode="json", by_alias=True)["pr"]["automatedCleanup"] is False


def test_multiple_invalid_sections_identify_their_own_source(tmp_repo, tmp_path):
    user_dir = tmp_path / "userconf"
    user_dir.mkdir()
    user_file = user_dir / "config.toml"
    repo_file = tmp_repo / ".agentic-preflight.toml"
    user_file.write_text("[gate]\nmode = 'invalid'\n")
    repo_file.write_text("[pr]\nmode = 'invalid'\n")
    with pytest.raises(ConfigError) as error:
        load_config(tmp_repo, user_config_dir=user_dir)
    message = str(error.value)
    assert f"in {user_file}: [gate] mode" in message
    assert f"in {repo_file}: [pr] mode" in message
    assert "\ninvalid configuration in " in message


def test_root_validation_error_keeps_default_source(tmp_path):
    with pytest.raises(ValidationError) as error:
        Config.model_validate([])
    source = tmp_path / "config.toml"
    message = _describe(error.value, {}, source)
    assert f"invalid configuration in {source}: <root>:" in message


def test_empty_validation_errors_still_have_a_message(tmp_path):
    error = ValidationError.from_exception_data("Config", [])
    source = tmp_path / "config.toml"
    assert _describe(error, {}, source) == f"invalid configuration in {source}: validation failed"
