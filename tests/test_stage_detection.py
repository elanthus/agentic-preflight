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
    ]
    assert candidates[0].as_dict() == {
        "command": "pytest",
        "source": "pyproject.toml",
    }
    assert all("workflow" not in item.source for item in candidates)


def test_stage_detection_ignores_malformed_manifests(tmp_path):
    (tmp_path / "package.json").write_text("not json")
    (tmp_path / "pyproject.toml").write_text("not = [valid")
    assert candidates_for(tmp_path, "test") == []


def test_stage_detection_deduplicates_matching_commands(tmp_path):
    (tmp_path / "justfile").write_text("test:\n    pytest\ntest:\n    pytest\n")
    assert [item.command for item in candidates_for(tmp_path, "test")] == ["just test"]


def test_stage_detection_does_not_scrape_workflows(tmp_path):
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yaml").write_text("steps:\n  run: pytest integration-tests\n")
    assert candidates_for(tmp_path, "test") == []


def test_stage_detection_tolerates_non_utf8_sources(tmp_path):
    (tmp_path / "Makefile").write_bytes(b"test:\n\techo caf\xe9\n")
    (tmp_path / "pyproject.toml").write_bytes(b"# caf\xe9\n[tool.pytest]\n")

    assert candidates_for(tmp_path, "test") == []


def test_stage_detection_ignores_package_json_without_object_top_level(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps(["test"]))
    assert candidates_for(tmp_path, "test") == []

    (tmp_path / "package.json").write_text(json.dumps({"scripts": ["test"]}))
    assert candidates_for(tmp_path, "test") == []


def test_stage_detection_matches_stage_as_whole_word(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"scripts": {"latest": "x", "contest": "x", "test:unit": "x", "unit-tests": "x"}}
        )
    )
    (tmp_path / "Makefile").write_text("latest:\n\ttrue\ntest:\n\ttrue\n")

    commands = [item.command for item in candidates_for(tmp_path, "test")]

    assert commands == ["npm run test:unit", "npm run unit-tests", "make test"]
