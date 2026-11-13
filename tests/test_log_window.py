"""Bounded windows over a stored stage log, as `logs` returns them."""

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentic_preflight.runs.log_window import read_log_window
from agentic_preflight.stages.shellstage import REDACTED


def _write(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "lint.txt"
    path.write_bytes(content)
    return path


def test_a_byte_cut_never_splits_a_multibyte_character_at_the_start(tmp_path):
    # Each "é" is two bytes; an odd byte budget would land mid-character.
    path = _write(tmp_path, "é".encode() * 100)

    window = read_log_window(path, tail=1, max_bytes=11)

    assert window.output == "é" * 5
    assert window.truncated is True
    assert "�" not in window.output


def test_a_byte_cut_never_splits_a_multibyte_character_at_the_end(tmp_path):
    path = _write(tmp_path, "€".encode() * 100)

    window = read_log_window(path, head=1, max_bytes=8)

    assert window.output == "€" * 2
    assert "�" not in window.output


@pytest.mark.parametrize("cut", range(1, len(REDACTED)))
def test_a_tail_cut_drops_a_split_redaction_marker(tmp_path, cut):
    tail = f"{REDACTED}after".encode()
    path = _write(tmp_path, b"before " + tail)

    window = read_log_window(path, tail=1, max_bytes=len(tail) - cut)

    assert window.output == "after"


@pytest.mark.parametrize("cut", range(1, len(REDACTED)))
def test_a_head_cut_drops_a_split_redaction_marker(tmp_path, cut):
    head = f"before{REDACTED}".encode()
    path = _write(tmp_path, head + b" after")

    window = read_log_window(path, head=1, max_bytes=len(head) - cut)

    assert window.output == "before"


def test_a_whole_marker_at_the_cut_is_kept(tmp_path):
    path = _write(tmp_path, f"before{REDACTED}after".encode())

    assert read_log_window(path, head=1, max_bytes=6 + len(REDACTED)).output == (
        f"before{REDACTED}"
    )
    assert read_log_window(path, tail=1, max_bytes=5 + len(REDACTED)).output == (f"{REDACTED}after")


def test_line_counts_include_an_unterminated_last_line(tmp_path):
    window = read_log_window(_write(tmp_path, b"a\nb\nc"), tail=2)

    assert window.output == "b\nc"
    assert window.total_lines == 3
    assert window.total_bytes == 5


def test_an_empty_log(tmp_path):
    window = read_log_window(_write(tmp_path, b""), tail=200)

    assert window.output == ""
    assert window.truncated is False
    assert window.total_lines == 0


@settings(max_examples=200, deadline=None)
@given(
    text=st.lists(st.sampled_from(["a", "\n", "é", "€", "😀", REDACTED]), max_size=30).map("".join),
    lines=st.integers(min_value=1, max_value=10),
    max_bytes=st.integers(min_value=1, max_value=80),
    from_head=st.booleans(),
)
def test_a_window_is_always_a_valid_contiguous_slice(
    tmp_path_factory, text, lines, max_bytes, from_head
):
    content = text.encode()
    path = tmp_path_factory.mktemp("log") / "lint.txt"
    path.write_bytes(content)

    if from_head:
        window = read_log_window(path, head=lines, max_bytes=max_bytes)
    else:
        window = read_log_window(path, tail=lines, max_bytes=max_bytes)

    returned = window.output.encode()
    assert len(returned) <= max_bytes
    assert "�" not in window.output
    assert content.startswith(returned) if from_head else content.endswith(returned)
    assert window.truncated == (returned != content)
    # No redaction marker straddles the cut.
    boundary = len(returned) if from_head else len(content) - len(returned)
    marker = REDACTED.encode()
    index = content.find(marker)
    while index != -1:
        assert not index < boundary < index + len(marker)
        index = content.find(marker, index + 1)
