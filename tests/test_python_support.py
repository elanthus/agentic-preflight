"""Pin the supported Python versions across packaging metadata, docs, and CI.

The classifiers in pyproject.toml are the source of truth. Every other place that
names the supported versions must agree with them, so a version cannot be declared
without being tested, or tested without being declared.
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _supported() -> list[str]:
    prefix = "Programming Language :: Python :: 3."
    minors = sorted(
        int(classifier.removeprefix(prefix))
        for classifier in PYPROJECT["project"]["classifiers"]
        if classifier.startswith(prefix)
    )
    assert minors, "pyproject.toml declares no Python minor-version classifiers"
    assert minors == list(range(minors[0], minors[-1] + 1)), "classifiers skip a version"
    return [f"3.{minor}" for minor in minors]


SUPPORTED = _supported()
MINIMUM = SUPPORTED[0]
MAXIMUM = SUPPORTED[-1]
FIRST_UNSUPPORTED = f"3.{int(MAXIMUM.split('.')[1]) + 1}"


def _english_list(items: list[str]) -> str:
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _workflow_matrices(filename: str) -> list[list[str]]:
    """Return every JSON array passed as a python-versions input in a workflow."""
    workflow = (WORKFLOWS / filename).read_text(encoding="utf-8")
    lines = [line for line in workflow.splitlines() if line.strip().startswith("python-versions:")]
    assert lines, f"{filename} passes no python-versions input"
    matrices = []
    for line in lines:
        matrices.extend(json.loads(array) for array in re.findall(r"'(\[[^']*\])'", line))
    return matrices


def test_requires_python_is_capped_at_the_first_untested_minor():
    assert PYPROJECT["project"]["requires-python"] == f">={MINIMUM},<{FIRST_UNSUPPORTED}"


def test_lockfile_matches_requires_python():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert lock["requires-python"] == f">={MINIMUM}, <{FIRST_UNSUPPORTED}"


def test_tooling_targets_the_minimum_version():
    assert PYPROJECT["tool"]["ruff"]["target-version"] == "py" + MINIMUM.replace(".", "")
    assert PYPROJECT["tool"]["mypy"]["python_version"] == MINIMUM


def test_compatibility_policy_names_the_supported_versions_and_the_cap():
    compatibility = (ROOT / "COMPATIBILITY.md").read_text(encoding="utf-8")
    assert f"supports Python {_english_list(SUPPORTED)} on" in compatibility
    assert f'requires-python = ">={MINIMUM},<{FIRST_UNSUPPORTED}"' in compatibility
    assert f"Python {MINIMUM} and Python {MAXIMUM}" in compatibility


def test_readme_names_the_supported_range():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"Python {MINIMUM} through {MAXIMUM}" in readme
    badge = "%20%7C%20".join(SUPPORTED)
    assert f"badge/python-{badge}-blue" in readme


def test_pull_requests_test_the_minimum_and_maximum_versions():
    dispatch, pull_request = _workflow_matrices("ci.yml")
    assert dispatch == SUPPORTED
    assert MINIMUM in pull_request
    assert MAXIMUM in pull_request
    assert set(pull_request) <= set(SUPPORTED)


def test_coverage_badge_is_produced_by_exactly_one_matrix_leg():
    # ci.yml passes several Python versions on push; every badge step must pin
    # itself to one OS and one Python version, or two legs upload an artifact
    # with the same name and upload-artifact fails the job.
    workflow = (WORKFLOWS / "test.yml").read_text(encoding="utf-8")
    steps = workflow.split("      - name: ")
    badge_steps = [step for step in steps if "coverage badge" in step.splitlines()[0]]
    assert len(badge_steps) == 2
    for step in badge_steps:
        condition = next(line for line in step.splitlines() if line.strip().startswith("if:"))
        assert "runner.os == 'Linux'" in condition
        assert "matrix.python-version == fromJSON(inputs.python-versions)[0]" in condition


def test_releases_test_every_supported_version():
    assert _workflow_matrices("release.yml") == [SUPPORTED]


def test_wheel_smoke_test_covers_the_minimum_and_maximum_versions():
    smoke = (ROOT / "scripts" / "smoke-wheel.sh").read_text(encoding="utf-8")
    match = re.search(r"^for python in (.+); do$", smoke, re.MULTILINE)
    assert match, "smoke-wheel.sh does not loop over Python versions"
    assert match.group(1).split() == [MINIMUM, MAXIMUM]
    assert 'uv tool install --python "$python"' in smoke
