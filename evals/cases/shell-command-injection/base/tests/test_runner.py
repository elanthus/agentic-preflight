from deploy.runner import deploy


def test_api_is_callable():
    assert callable(deploy)
