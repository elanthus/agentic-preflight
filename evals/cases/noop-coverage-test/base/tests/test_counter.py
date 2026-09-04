from counter.core import increment


def test_increment():
    assert increment(2) == 3
