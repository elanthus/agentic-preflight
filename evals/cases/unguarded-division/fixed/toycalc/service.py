def divide(numerator: float, divisor: float) -> float:
    """Divide while giving callers a stable domain error."""
    if divisor == 0:
        raise ValueError("divisor must be non-zero")
    return numerator / divisor
