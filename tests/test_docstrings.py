from repoutils.docstrings import summary


def test_summary_is_the_first_paragraph_on_one_line() -> None:
    assert summary("Check that\nthings hold.\n\nMore detail.\n") == "Check that things hold."


def test_summary_of_a_one_line_docstring() -> None:
    assert summary("Check things.") == "Check things."


def test_summary_without_a_docstring() -> None:
    assert summary(None) is None
