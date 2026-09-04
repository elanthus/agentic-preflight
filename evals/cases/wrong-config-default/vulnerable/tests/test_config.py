from worker.config import WorkerConfig


def test_timeout_default():
    assert WorkerConfig().timeout_seconds == 30
