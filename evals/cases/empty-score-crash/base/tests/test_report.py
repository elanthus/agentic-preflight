from scores.report import highest


def test_empty_scores():
    assert highest([]) is None
