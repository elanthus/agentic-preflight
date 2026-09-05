import pytest

from agentic_preflight.codeowners import matches
from agentic_preflight.grounding import _codeowners_entries


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*", "nested/file.py", True),
        ("*.js", "src/nested/app.js", True),
        ("/build/logs/", "build/logs/deep/file.log", True),
        ("/build/logs/", "other/build/logs/file.log", False),
        ("docs/*", "docs/guide.md", True),
        ("docs/*", "docs/build-app/guide.md", False),
        ("apps/", "nested/apps/api/main.py", True),
        ("**/logs", "logs/debug.log", True),
        ("**/logs", "nested/logs/debug.log", True),
        ("/apps/github", "apps/github/main.py", True),
        ("a/**/z.py", "a/z.py", True),
        ("a/**/z.py", "a/b/c/z.py", True),
        ("a/?.py", "a/xy.py", False),
        ("/src/*/", "src/module/deep/app.py", True),
        ("*.JS", "app.js", False),
        ("!docs/", "docs/guide.md", False),
        ("[ab].py", "a.py", False),
    ],
)
def test_github_codeowners_patterns(pattern, path, expected):
    assert matches(path, pattern) is expected


def test_empty_ownership_clears_an_earlier_rule_and_can_be_overridden():
    texts = {"CODEOWNERS": "* @global\n/docs/ # deliberately unowned\n/docs/special.md @special\n"}
    entries = _codeowners_entries(texts, ["docs/guide.md", "docs/special.md"], set(texts))
    assert {entry["path"]: entry["owners"] for entry in entries} == {
        "docs/guide.md": [],
        "docs/special.md": ["@special"],
    }


def test_omitted_first_codeowners_file_does_not_fall_back_to_another():
    assert (
        _codeowners_entries(
            {"CODEOWNERS": "* @fallback"}, ["app.py"], {".github/CODEOWNERS", "CODEOWNERS"}
        )
        == []
    )
