from pathlib import Path

import pytest

from agentic_preflight.stages.docs import build_inventory, changelog_finding
from tools.changelog import assemble


def fixture(root: Path) -> Path:
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Legacy fix.\n\n"
        "## [1.0.0] - 2026-01-01\n\nHistorical text.\n"
    )
    fragments = root / "docs/CHANGELOG.d"
    fragments.mkdir(parents=True)
    return fragments


def test_assembly_preserves_history_orders_entries_and_consumes_fragments(tmp_path):
    fragments = fixture(tmp_path)
    (fragments / "22.fixed.md").write_text("- Second fix.\n")
    (fragments / "11.fixed.md").write_text("- First fix.\n")
    (fragments / "feature.added.md").write_text(
        "- Feature.\n  Continued detail.\n\n- Another feature.\n"
    )
    assemble(tmp_path, "1.1.0", "2026-02-01")
    result = (tmp_path / "CHANGELOG.md").read_text()
    assert result == (
        "# Changelog\n\n## [Unreleased]\n\n## [1.1.0] - 2026-02-01\n\n"
        "### Added\n\n- Feature.\n  Continued detail.\n\n- Another feature.\n\n"
        "### Fixed\n\n- Legacy fix.\n\n"
        "- First fix.\n\n- Second fix.\n\n"
        "## [1.0.0] - 2026-01-01\n\nHistorical text.\n"
    )
    assert not list(fragments.iterdir())
    with pytest.raises(ValueError, match="already"):
        assemble(tmp_path, "1.1.0", "2026-02-01")
    assert (tmp_path / "CHANGELOG.md").read_text() == result


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("bad.md", "- Note."),
        ("1.fixed.md", "# Heading"),
        ("1.fixed.md", "- Feature.\n\nRelease notes"),
    ],
)
def test_invalid_fragment_leaves_all_inputs_untouched(tmp_path, name, text):
    fragments = fixture(tmp_path)
    (fragments / "0.added.md").write_text("- Valid.\n")
    (fragments / name).write_text(text)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="fragment"):
        assemble(tmp_path, "1.1.0", "2026-02-01")
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("date", ["20260201", "2026-W05-7"])
def test_noncanonical_date_leaves_all_inputs_untouched(tmp_path, date):
    fragments = fixture(tmp_path)
    (fragments / "1.changed.md").write_text("- Change.\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        assemble(tmp_path, "1.1.0", date)
    assert {p: p.read_bytes() for p in before} == before


def test_fragment_satisfies_existing_docs_changelog_check(tmp_path):
    fragments = fixture(tmp_path)
    (fragments / "42.changed.md").write_text("- Behavior change.\n")
    changed = ["docs/CHANGELOG.d/42.changed.md"]
    inventory = build_inventory(tmp_path, changed)
    assert changelog_finding(inventory, changed, finding_id="F001") is None
