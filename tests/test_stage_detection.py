import json

from agentic_preflight.stages.detect import candidates_for


def test_stage_detection_combines_sources_in_priority_order(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test:unit": "pytest", "build": "echo build"}})
    )
    (tmp_path / "justfile").write_text("test-fast:\n    pytest -q\n")
    (tmp_path / "Makefile").write_text("test-all:\n\tpytest\n")
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text("steps:\n  run: pytest integration-tests\n")

    candidates = candidates_for(tmp_path, "test")

    assert [item.command for item in candidates] == [
        "pytest",
        "npm run test:unit",
        "just test-fast",
        "make test-all",
        "pytest integration-tests",
    ]
    assert candidates[0].as_dict() == {
        "command": "pytest",
        "source": "pyproject.toml",
    }


def test_stage_detection_ignores_malformed_manifests(tmp_path):
    (tmp_path / "package.json").write_text("not json")
    (tmp_path / "pyproject.toml").write_text("not = [valid")
    assert candidates_for(tmp_path, "test") == []


def test_stage_detection_deduplicates_matching_commands(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yaml").write_text("steps:\n  run: pytest\n")

    candidates = candidates_for(tmp_path, "test")

    assert [item.command for item in candidates] == ["pytest"]
