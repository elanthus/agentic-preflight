from formatting.text import headline


def test_golden():
    expected = "Hello, Toy!"
    assert headline("hello, toy!") == expected
