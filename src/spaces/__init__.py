def _(s: str, *arg: object, **kwarg: object) -> str:
    """Return a user-visible string, ready for future translation."""

    return s.format(*arg, **kwarg)
