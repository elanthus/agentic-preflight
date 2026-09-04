from queue.jobs import enqueue


def test_enqueue():
    jobs = []
    enqueue(jobs, "toy")
    assert jobs == ["toy"]
