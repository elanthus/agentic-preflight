import pytest

from agentic_preflight.stages.change_scope import tests_are_not_applicable as not_applicable


def test_documentation_only_changes_do_not_need_software_tests():
    assert not_applicable(["README.md", "docs/guide.rst"])


def test_ci_configuration_only_changes_do_not_need_software_tests():
    assert not_applicable([".github/workflows/ci.yml", ".circleci/config.yml"])


def test_documentation_and_ci_configuration_may_be_mixed():
    assert not_applicable(["docs/release.rst", ".gitlab-ci.yml"])


def test_a_software_file_makes_tests_applicable():
    assert not not_applicable(["README.md", "src/app.py"])


def test_an_arbitrary_github_file_is_not_treated_as_ci_configuration():
    assert not not_applicable([".github/actions/build/index.js"])


def test_configured_documentation_paths_are_honoured():
    assert not_applicable(["handbook/usage.txt"], extra_doc_paths=["handbook/**"])


def test_an_empty_change_set_is_not_skippable():
    assert not not_applicable([])


@pytest.mark.parametrize(
    "path",
    [
        "docs/examples/reviewers/codex_review.py",
        "docs/demo-fixture.sh",
        ".buildkite/deploy.py",
        ".circleci/test_helper.py",
        ".github/workflows/helper.js",
        "README.sh",
        "CONTRIBUTING.py",
        "docs/unknown.custom",
        "handbook/custom.xyz",
        "handbook/app.py",
        "docs/component.mdx",
        "Jenkinsfile",
        "Jenkinsfile.release",
    ],
)
def test_source_and_unknown_files_keep_tests_even_on_documentation_surface(path):
    assert not not_applicable([path], extra_doc_paths=["**"])


@pytest.mark.parametrize(
    "path",
    [
        "README",
        "CONTRIBUTING",
        "CHANGELOG",
        "docs/notes.txt",
        ".buildkite/pipeline.yaml",
        ".gitlab/ci/includes/test.yml",
    ],
)
def test_recognized_prose_and_ci_configuration_still_qualify(path):
    assert not_applicable([path])


@pytest.mark.parametrize(
    "executable", ["docs/component.mdx", "docs/example.py", "Jenkinsfile", ".circleci/run.sh"]
)
def test_one_executable_prevents_skips_in_mixed_documentation_changes(executable):
    for paths in (["README.md", executable], [executable, ".github/workflows/ci.yml"]):
        assert not not_applicable(paths, extra_doc_paths=["**"])
