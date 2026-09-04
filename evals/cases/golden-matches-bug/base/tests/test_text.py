from formatting.text import headline


def test_golden():
    assert headline("hello, toy!") == "Hello, Toy!"
