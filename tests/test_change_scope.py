from agentic_preflight.stages.change_scope import tests_are_not_applicable as not_applicable


def test_documentation_only_changes_do_not_need_software_tests():
    assert not_applicable(["README.md", "docs/guide.mdx"])


def test_ci_configuration_only_changes_do_not_need_software_tests():
    assert not_applicable([".github/workflows/ci.yml", ".circleci/config.yml", "Jenkinsfile"])


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
