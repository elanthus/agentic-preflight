from counter.core import increment


def test_increment():
    assert increment(2) == 3


def test_increment_many_values():
    for value in (-1, 0, 41):
        assert increment(value) == value + 1
