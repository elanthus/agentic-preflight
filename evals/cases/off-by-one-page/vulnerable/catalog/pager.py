def page(items: list[str], number: int, size: int) -> list[str]:
    """Return one one-indexed page."""
    start = (number - 1) * size
    end = start + size
    return items[start : end - 1]
