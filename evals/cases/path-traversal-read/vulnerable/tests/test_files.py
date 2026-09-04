from notebook.files import read_note


def test_reader_exists():
    assert callable(read_note)
