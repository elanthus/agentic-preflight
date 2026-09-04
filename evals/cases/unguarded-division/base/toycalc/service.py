def divide(numerator: float, divisor: float) -> float:
    """Divide two numeric inputs safely."""
    if divisor == 0:
        raise ValueError("divisor must be non-zero")
    return numerator / divisor
