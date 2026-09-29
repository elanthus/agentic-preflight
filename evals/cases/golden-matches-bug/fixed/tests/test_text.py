from pathlib import Path

from formatting.text import headline

GOLDEN = Path(__file__).with_name("golden.txt").read_text(encoding="utf-8").splitlines()


def test_golden():
    assert [headline(value) for value in ("hello, toy!", "goodbye, toy.")] == GOLDEN
