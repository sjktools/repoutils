"""The summary each command's ``--help`` shows, taken from its module docstring."""


def summary(doc: str | None) -> str | None:
    """The docstring's first paragraph on one line, or None if there is none.

    ``python -OO`` strips docstrings, leaving ``__doc__`` None, and ``--help`` then
    shows no description rather than failing.
    """
    if doc is None:
        return None
    return " ".join(doc.strip().split("\n\n")[0].split())
