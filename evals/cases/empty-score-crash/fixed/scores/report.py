def highest(scores: list[int]) -> int | None:
    """Return the highest score when one exists."""
    if not scores:
        return None
    return max(scores)
