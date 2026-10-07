"""Safe operator-facing failures."""


class PlayError(Exception):
    """Messages never contain credential values, raw provider bodies or review text."""
