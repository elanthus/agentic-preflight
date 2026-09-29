def page(items: list[str], number: int, size: int) -> list[str]:
    """Return one one-indexed page."""
    start = (number - 1) * size
    end = min(start + size, len(items))
    return items[start:end]
