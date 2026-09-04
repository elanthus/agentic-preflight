from auth.check import accepted


def test_good_token():
    assert accepted("toy-token")
