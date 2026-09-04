from slug.format import slugify


def test_slug_fixture():
    expected = "toy-name"
    assert slugify("Toy Name") == expected
