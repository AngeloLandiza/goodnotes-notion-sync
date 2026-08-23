import pytest

from goodnotes_notion_sync.matching import (
    Candidate,
    best_match,
    course_code,
    normalize,
    score,
)


def pdf(name: str, ident: str | None = None) -> Candidate:
    return Candidate(id=ident or name, name=name, url=f"https://drive/{ident or name}")


# -- normalize --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CS 411 | Homework 3.pdf", "cs 411 homework 3"),
        ("CS411-Homework-3.pdf", "cs 411 homework 3"),
        ("CS 411 Homework 3 (2).pdf", "cs 411 homework 3"),
        ("CS 411 Homework 3 2026-09-14.pdf", "cs 411 homework 3"),
        ("HW 4", "homework 4"),
        ("hw4", "homework 4"),
        ("Artificial Intelligence I", "artificial intelligence 1"),
        ("Problem Set 07", "problemset 7"),
        ("  Café  Notes  ", "cafe"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_normalize_drops_noise_words():
    assert "goodnotes" not in normalize("STAT 382 Goodnotes Notes.pdf")


# -- course codes -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CS 411 | Homework 3", "CS 411"),
        ("cs411 homework", "CS 411"),
        ("STAT-382 Quiz 2.pdf", "STAT 382"),
        ("Homework 3", None),
        ("IDS 435 vs IDS 312", "IDS 435"),
    ],
)
def test_course_code(raw, expected):
    assert course_code(raw) == expected


# -- scoring ----------------------------------------------------------------


def test_identical_titles_score_one():
    assert score("CS 411 | Homework 3", "CS 411 Homework 3.pdf") == 1.0


def test_course_conflict_is_a_hard_veto():
    assert score("CS 411 | Homework 3", "CS 412 Homework 3.pdf") == 0.0


def test_different_numbers_are_penalised():
    same = score("CS 411 | Homework 3", "CS 411 Homework 3.pdf")
    other = score("CS 411 | Homework 3", "CS 411 Homework 4.pdf")
    assert other < same
    assert other < 0.78


def test_abbreviation_still_matches():
    assert score("CS 411 | HW 4", "CS 411 Homework 4.pdf") >= 0.78


def test_prefix_only_on_one_side_still_matches():
    assert score("CS 411 | Homework 3", "Homework 3.pdf") >= 0.78


def test_unrelated_files_score_low():
    assert score("CS 411 | Homework 3", "Grocery list.pdf") < 0.4


# -- best_match -------------------------------------------------------------


def test_best_match_picks_the_right_course():
    candidates = [
        pdf("CS 412 Homework 3.pdf"),
        pdf("CS 411 Homework 3.pdf"),
        pdf("STAT 382 Homework 3.pdf"),
    ]
    result = best_match("CS 411 | Homework 3", candidates)
    assert result.matched
    assert result.candidate.name == "CS 411 Homework 3.pdf"


def test_best_match_refuses_ambiguity():
    candidates = [pdf("Homework 3.pdf", "a"), pdf("Homework 3 copy.pdf", "b")]
    result = best_match("Homework 3", candidates)
    assert not result.matched
    assert "ambiguous" in result.reason


def test_best_match_with_no_candidates():
    result = best_match("CS 411 | Homework 3", [])
    assert not result.matched
    assert result.score == 0.0


def test_best_match_below_threshold_explains_itself():
    result = best_match("CS 411 | Homework 3", [pdf("Grocery list.pdf")])
    assert not result.matched
    assert "scored" in result.reason


def test_duplicate_suffix_file_still_wins():
    result = best_match(
        "STAT 382 | Quiz 2", [pdf("STAT 382 Quiz 2 (1).pdf"), pdf("Random.pdf")]
    )
    assert result.matched
    assert result.candidate.name == "STAT 382 Quiz 2 (1).pdf"
