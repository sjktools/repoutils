"""Check Markdown prose for doubled spaces between words, or collapse them with ``--fix``.

The files are ``[tool.repoutils.doubled-spaces]``'s ``files`` (default
``AGENTS.md`` and ``README.md``); a missing one is skipped.

A file that can't be read (one that isn't valid UTF-8, for example) is
reported, and fails the check.

It reports each line of prose that has two or more spaces between two
characters that aren't spaces. Code blocks, tables, headings, block quotes,
HTML blocks, link definitions, comments, code spans, inline HTML tags, and
link and image destinations aren't prose, so a doubled space in them is left
alone. With ``allow-after-sentence = true``,
two spaces after a sentence's closing punctuation are allowed.
"""

import argparse
import re
from pathlib import Path

from repoutils import config
from repoutils.docstrings import summary
from repoutils.markdown import MASK, WRAPPED_CODE_RE, UnreadableError, prose_blocks, read_text

DEFAULT_FILES = ["AGENTS.md", "README.md"]
WHERE = "[tool.repoutils.doubled-spaces]"
# Two or more spaces between two characters that aren't spaces.
DOUBLED_RE = re.compile(r"(?<=\S) {2,}(?=\S)")
# Inline markup that isn't shown as prose: a link's or image's destination and title, or an HTML tag or autolink.
MARKUP_RE = re.compile(r"\]\([^)]*\)|</?[a-zA-Z][^>]*>")
# Text that ends with a sentence's closing punctuation, and any closing quotes, brackets or emphasis.
SENTENCE_END_RE = re.compile(r"[.!?][\"')\]’”*_]*$")


def _mask(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", MASK, match[0])


def doubled_spaces(text: str, *, allow_after_sentence: bool = False) -> dict[int, list[tuple[int, int]]]:
    """Each doubled space in the document's prose, as its (start, end) columns, by line number (from 1)."""
    found: dict[int, list[tuple[int, int]]] = {}
    for block in prose_blocks(text):
        # A code span can run over wrapped lines, so mask the spans in the block as a whole.
        unspanned = WRAPPED_CODE_RE.sub(_mask, "\n".join(line.text for line in block)).split("\n")
        for line, prose in zip(block, (MARKUP_RE.sub(_mask, text) for text in unspanned), strict=True):
            for match in DOUBLED_RE.finditer(prose):
                if allow_after_sentence and len(match[0]) == 2 and SENTENCE_END_RE.search(prose, 0, match.start()):
                    continue
                found.setdefault(line.number, []).append((line.start + match.start(), line.start + match.end()))
    return found


def collapse(text: str, *, allow_after_sentence: bool = False) -> tuple[str, int]:
    """The document with each doubled space in its prose made one space, and how many there were."""
    found = doubled_spaces(text, allow_after_sentence=allow_after_sentence)
    lines = text.splitlines(keepends=True)
    for number, spans in found.items():
        line = lines[number - 1]
        for start, end in reversed(spans):
            line = line[:start] + " " + line[end:]
        lines[number - 1] = line
    return "".join(lines), sum(len(spans) for spans in found.values())


def files() -> list[str]:
    """The files to check, from config."""
    return config.string_list(config.section("doubled-spaces"), "files", DEFAULT_FILES, WHERE)


def allowed_after_sentence() -> bool:
    """Whether config allows two spaces after a sentence."""
    return config.boolean(config.section("doubled-spaces"), "allow-after-sentence", False, WHERE)


def main(paths: list[str] | None = None, *, fix: bool = False, allow_after_sentence: bool = False) -> int:
    if paths is None:
        paths = [path for path in files() if Path(path).is_file()]
    failures: list[str] = []
    collapsed = 0
    for path in paths:
        try:
            # Keep the file's line endings, so --fix writes back only the spaces it changes.
            text = read_text(path, newline="")
        except UnreadableError as error:
            failures.append(str(error))
            continue
        if fix:
            fixed, count = collapse(text, allow_after_sentence=allow_after_sentence)
            if count:
                try:
                    with open(path, "w", encoding="utf-8", newline="") as file:
                        file.write(fixed)
                except OSError as error:
                    failures.append(f"{path}: cannot write ({error.strerror or error})")
                    continue
                print(f"{path}: collapsed {count} doubled space{'s' if count != 1 else ''}.")
            collapsed += count
            continue
        lines = text.splitlines()
        for number in doubled_spaces(text, allow_after_sentence=allow_after_sentence):
            failures.append(f"{path}:{number}: {lines[number - 1].strip()}")
    if failures:
        print("Doubled spaces check failed:" if not fix else "Doubled spaces fix failed:")
        for failure in failures:
            print(f"- {failure}")
        if not fix:
            print("Run `repoutils check-doubled-spaces --fix` to collapse each doubled space to one.")
        return 1
    if not collapsed:
        print(f"{', '.join(paths) or 'No files'}: no doubled spaces in the prose.")
    return 0


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="repoutils check-doubled-spaces", description=summary(__doc__))
    parser.add_argument("files", nargs="*", metavar="FILE", help="files to check (default: configured files)")
    parser.add_argument("--fix", action="store_true", help="collapse each doubled space to one space")
    parser.add_argument(
        "--allow-after-sentence",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="allow two spaces after a sentence's closing punctuation (default: allow-after-sentence from config)",
    )
    args = parser.parse_args(argv)
    for path in args.files:
        if not Path(path).is_file():
            parser.error(f"{path} is not a file")
    allow = allowed_after_sentence() if args.allow_after_sentence is None else args.allow_after_sentence
    return main(args.files or None, fix=args.fix, allow_after_sentence=allow)
