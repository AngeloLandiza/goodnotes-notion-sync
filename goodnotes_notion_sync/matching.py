"""Filename <-> assignment-title matching.

GoodNotes exports a notebook as ``<Notebook name>.pdf``. People do not name
notebooks with the same punctuation they use in Notion, and the Drive backup
adds its own noise (``" (1)"`` on collisions, occasional date stamps). This
module normalises both sides and scores them, with a hard veto when the two
strings clearly belong to different courses.

Only the standard library is used so the tool has no fuzzy-matching dependency.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence

__all__ = [
    "Candidate",
    "MatchResult",
    "course_code",
    "normalize",
    "score",
    "best_match",
]

# "CS 411", "CS411", "cs-411", "STAT  382"
_COURSE_RE = re.compile(r"\b([a-z]{2,5})[\s\-_]*([0-9]{3})\b")

# Trailing " (1)", " (2)" that Drive appends to duplicate filenames.
_DUPLICATE_SUFFIX_RE = re.compile(r"\s*\((\d{1,3})\)\s*$")

# Date stamps GoodNotes/Drive sometimes append: 2026-09-14, 2026_09_14, 09-14-26
_DATE_SUFFIX_RE = re.compile(
    r"[\s\-_]*(?:\d{4}[\-_./]\d{1,2}[\-_./]\d{1,2}|\d{1,2}[\-_./]\d{1,2}[\-_./]\d{2,4})\s*$"
)

# Words that carry no signal and only inflate similarity scores.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "copy",
        "for",
        "goodnotes",
        "notes",
        "of",
        "pdf",
        "the",
    }
)

# Tokens that mean the same thing on either side of the match.
_SYNONYMS = {
    "hw": "homework",
    "assn": "assignment",
    "prob": "problem",
    "probs": "problems",
    "pset": "problemset",
    "proj": "project",
    "lab": "lab",
    "mt": "midterm",
    "exam": "exam",
    "quiz": "quiz",
    "no": "",
    "num": "",
    "number": "",
}

_ROMAN = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
}


@dataclass(frozen=True)
class Candidate:
    """A file that could back an assignment."""

    id: str
    name: str
    url: str
    path: str = ""
    modified_time: str = ""


@dataclass(frozen=True)
class MatchResult:
    candidate: Candidate | None
    score: float
    runner_up: float = 0.0
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.candidate is not None


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def course_code(text: str) -> str | None:
    """Return a canonical ``"CS 411"`` style code, or ``None``.

    Only the *first* code in the string counts. A title like
    ``"CS 411 | compare with CS 412"`` belongs to CS 411.
    """
    match = _COURSE_RE.search(_strip_accents(text).lower())
    if match is None:
        return None
    return f"{match.group(1).upper()} {match.group(2)}"


# Multi-word forms that should collapse to the single token used elsewhere,
# so "pset 7" and "problem set 7" compare equal.
_PHRASES = [
    (re.compile(r"\bproblem\s+set\b"), "problemset"),
    (re.compile(r"\bp\s+set\b"), "problemset"),
    (re.compile(r"\bhome\s+work\b"), "homework"),
    (re.compile(r"\bmid\s+term\b"), "midterm"),
    (re.compile(r"\bdiscussion\s+board\b"), "discussionboard"),
]


def normalize(text: str) -> str:
    """Reduce a filename or assignment title to comparable words."""
    text = _strip_accents(text).lower()

    # Drop a trailing file extension (".pdf", ".goodnotes").
    text = re.sub(r"\.[a-z0-9]{1,10}$", "", text)

    text = _DUPLICATE_SUFFIX_RE.sub("", text)
    text = _DATE_SUFFIX_RE.sub("", text)

    # Every separator becomes a space.
    text = re.sub(r"[^a-z0-9]+", " ", text)

    for pattern, replacement in _PHRASES:
        text = pattern.sub(replacement, text)

    tokens: list[str] = []
    for token in text.split():
        token = _SYNONYMS.get(token, token)
        if not token or token in _STOPWORDS:
            continue
        token = _ROMAN.get(token, token)
        # "hw4" -> "hw 4" so it lines up with "hw 4"
        split = re.match(r"^([a-z]+)(\d+)$", token)
        if split:
            head = _SYNONYMS.get(split.group(1), split.group(1))
            if head and head not in _STOPWORDS:
                tokens.append(head)
            tokens.append(split.group(2).lstrip("0") or "0")
            continue
        if token.isdigit():
            token = token.lstrip("0") or "0"
        tokens.append(token)

    return " ".join(tokens)


def _token_set_ratio(left: str, right: str) -> float:
    """Order-insensitive similarity, in the spirit of ``fuzz.token_set_ratio``."""
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0

    shared = left_tokens & right_tokens
    only_left = left_tokens - right_tokens
    only_right = right_tokens - left_tokens

    base = " ".join(sorted(shared))
    combined_left = (base + " " + " ".join(sorted(only_left))).strip()
    combined_right = (base + " " + " ".join(sorted(only_right))).strip()

    return max(
        SequenceMatcher(None, base, combined_left).ratio(),
        SequenceMatcher(None, base, combined_right).ratio(),
        SequenceMatcher(None, combined_left, combined_right).ratio(),
    )


def score(title: str, filename: str) -> float:
    """Score how well ``filename`` backs the assignment ``title`` (0.0 - 1.0).

    A course-code conflict is a veto, not a penalty: "CS 411 Homework 3" must
    never resolve to a "CS 412 Homework 3" notebook, however similar the rest
    of the string looks.
    """
    title_code = course_code(title)
    file_code = course_code(filename)
    if title_code and file_code and title_code != file_code:
        return 0.0

    normalized_title = normalize(title)
    normalized_file = normalize(filename)
    if not normalized_title or not normalized_file:
        return 0.0

    if normalized_title == normalized_file:
        return 1.0

    plain = SequenceMatcher(None, normalized_title, normalized_file).ratio()
    token = _token_set_ratio(normalized_title, normalized_file)
    result = max(plain, token)

    # One string fully containing the other is a strong signal that survives
    # extra words like a course prefix on only one side.
    if normalized_title in normalized_file or normalized_file in normalized_title:
        result = max(result, 0.92)

    # Agreeing on the course code is corroboration, not proof.
    if title_code and file_code and title_code == file_code:
        result = min(1.0, result + 0.04)

    # Disagreeing on an assignment number is usually a different assignment.
    # The course number ("411") must not count here, or "CS 411 | Homework 3"
    # would look like it disagrees with "Homework 3.pdf".
    title_numbers = _content_numbers(normalized_title, title_code)
    file_numbers = _content_numbers(normalized_file, file_code)
    if title_numbers and file_numbers and title_numbers != file_numbers:
        result -= 0.25

    # Only an identical normalised string earns a perfect score. A subset match
    # ("Homework 3" inside "CS 411 Homework 3") otherwise ties with the exact
    # title, and then whichever assignment is processed first claims the file.
    return max(0.0, min(0.97, result))


def _content_numbers(normalized: str, code: str | None) -> set[str]:
    """Numbers in the string, ignoring the ones that are the course code."""
    numbers = {token for token in normalized.split() if token.isdigit()}
    if code:
        numbers.discard(code.split()[1].lstrip("0") or "0")
    return numbers


def best_match(
    title: str,
    candidates: Sequence[Candidate] | Iterable[Candidate],
    *,
    threshold: float = 0.78,
    margin: float = 0.06,
) -> MatchResult:
    """Pick the single best candidate for ``title``.

    Returns an unmatched result when nothing clears ``threshold``, or when the
    top two candidates are within ``margin`` of each other -- an ambiguous
    match is worse than no match, because it writes a wrong link that looks
    right.
    """
    scored = sorted(
        ((score(title, c.name), c) for c in candidates),
        key=lambda pair: (-pair[0], pair[1].name),
    )
    if not scored:
        return MatchResult(None, 0.0, reason="no files in the Drive folder")

    top_score, top = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if top_score < threshold:
        return MatchResult(
            None,
            top_score,
            runner_up,
            reason=f"best candidate {top.name!r} scored {top_score:.2f} < {threshold:.2f}",
        )

    if top_score - runner_up < margin and len(scored) > 1:
        return MatchResult(
            None,
            top_score,
            runner_up,
            reason=(
                f"ambiguous: {top.name!r} ({top_score:.2f}) vs "
                f"{scored[1][1].name!r} ({runner_up:.2f})"
            ),
        )

    return MatchResult(top, top_score, runner_up, reason="ok")
