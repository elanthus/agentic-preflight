from catalog.pager import page


def test_page_starts_correctly():
    assert page(["a", "b", "c"], 1, 2)[0] == "a"
