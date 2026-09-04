from auth.check import accepted


def test_bad_token():
    assert not accepted("bad")
