from scores.report import highest


def test_nonempty_scores():
    assert highest([2, 4]) == 4
