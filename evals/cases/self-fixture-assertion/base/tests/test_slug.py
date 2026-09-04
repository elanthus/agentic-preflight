from slug.format import slugify


def test_slug_output():
    expected = "toy-name"
    assert slugify("Toy Name") == expected
