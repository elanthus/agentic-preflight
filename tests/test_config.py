import pytest

from agentic_preflight.config import Config, ConfigError, load_config


def test_defaults_apply_when_no_config_file_exists(tmp_repo, tmp_path):
    cfg = load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert cfg.general.base_ref == "main"
    assert cfg.review.blocking_severities == ["critical", "high"]
    assert cfg.docs.enabled is True
    assert cfg.worktree.copy_files == [".env"]
    assert cfg.worktree.root is None
    assert cfg.worktree.mode == "in_place"
    assert cfg.worktree.dependency_setup == "auto"
    assert cfg.runtime.manager == "auto"
    assert cfg.runtime.strict is True
    assert cfg.gate.mode == "token"
    assert cfg.pr.mode == "auto"
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


@pytest.mark.parametrize("pattern", ["", "/absolute/**", "src/../secrets/**"])
def test_policy_rejects_unsafe_patterns(tmp_repo, tmp_path, pattern):
    (tmp_repo / ".agentic-preflight.toml").write_text(
        f"[policy]\nhuman_review_paths = [{pattern!r}]\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "human_review_paths" in str(exc.value)


def test_runtime_manager_rejects_an_unknown_value(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text("[runtime]\nmanager = 'magic'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "magic" in str(exc.value)


def test_dependency_setup_rejects_an_unknown_mode(tmp_repo, tmp_path):
    (tmp_repo / ".agentic-preflight.toml").write_text(
        "[worktree]\ndependency_setup = 'sometimes'\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_repo, user_config_dir=tmp_path / "nowhere")
    assert "sometimes" in str(exc.value)


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
