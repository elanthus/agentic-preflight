from catalog.pager import page


def test_full_page():
    assert page(["a", "b", "c"], 1, 2) == ["a", "b"]
