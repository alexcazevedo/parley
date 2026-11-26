"""Text helpers of the knowledge base: normalisation for search, splitting into passages.

`normalize` lower-cases and strips accents so `horario` finds `horário`; the
same treatment is given to passages (`search_text`) and to queries.

`split_passages` packs paragraphs (separated by a blank line) into passages
of at most `PASSAGE_CHARS` characters together with the heading. A paragraph
longer than that is cut at sentence ends; a sentence longer still is cut at
the last space that fits (or hard at the limit).
"""

import re
import unicodedata

PASSAGE_CHARS = 700
# Least room left for content when the heading is long.
_MIN_CONTENT_CHARS = 200

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_SPACES = re.compile(r"[ \t\r\f\v]+")


def normalize(text: str) -> str:
    """Lower-cased, accents stripped, whitespace runs collapsed."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


def _cut_long(sentence: str, budget: int) -> list[str]:
    pieces: list[str] = []
    rest = sentence
    while len(rest) > budget:
        cut = rest.rfind(" ", 0, budget + 1)
        if cut <= 0:
            cut = budget
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def _units(paragraph: str, budget: int) -> list[str]:
    """`paragraph` as pieces of at most `budget` characters."""
    if len(paragraph) <= budget:
        return [paragraph]
    units: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        for piece in _cut_long(sentence, budget):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= budget:
                current = f"{current} {piece}"
            else:
                units.append(current)
                current = piece
    if current:
        units.append(current)
    return units


def split_passages(heading: str, body: str) -> list[str]:
    """The passage contents of `body`, each fitting `PASSAGE_CHARS` with `heading`."""
    budget = max(PASSAGE_CHARS - len(heading.strip()) - 1, _MIN_CONTENT_CHARS)
    paragraphs = [
        _SPACES.sub(" ", p).strip() for p in _PARAGRAPH_BREAK.split(body.replace("\r\n", "\n"))
    ]
    passages: list[str] = []
    current = ""
    for paragraph in (p for p in paragraphs if p):
        for unit in _units(paragraph, budget):
            if not current:
                current = unit
            elif len(current) + 2 + len(unit) <= budget:
                current = f"{current}\n\n{unit}"
            else:
                passages.append(current)
                current = unit
    if current:
        passages.append(current)
    return passages
