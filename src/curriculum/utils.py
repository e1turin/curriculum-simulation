def require(condition: object, message: str) -> None:
    """Raise ``ValueError`` when an input constraint is not satisfied."""
    if not condition:
        raise ValueError(message)


def require_type[T](value: object, expected: type[T], message: str) -> T:
    """Validate and narrow a runtime value to ``T``."""
    if not isinstance(value, expected):
        raise TypeError(message)
    return value
