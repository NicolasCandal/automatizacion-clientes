"""Name comparison against the current library without networking."""

from math import isfinite

import pytest

from client_automation.core.contracts import NameComparator
from client_automation.core.normalization import normalize
from client_automation.infra.rapidfuzz_comparator import RapidFuzzComparator


@pytest.fixture
def comparator() -> NameComparator:
    return RapidFuzzComparator()


@pytest.mark.parametrize(
    ("query", "candidate", "expected"),
    [
        ("axa sample", "axa sample", 100.0),
        ("sample axa", "axa sample", 100.0),
        ("firuso fivora", "firuso baleve fivora", 100.0),
        ("firuso baleve fivora", "firuso fivora", 100.0),
        ("axa axa sample", "axa sample", 100.0),
        ("aaa", "zzz", 0.0),
        ("axa", "axxa", 85.71428571428571),
        ("axa", "axaxa", 75.0),
        ("axefor", "axxefor", 92.3076923076923),
        ("", "", 0.0),
        ("", "axa sample", 0.0),
        ("axa sample", "", 0.0),
        (" \t ", " \n ", 0.0),
    ],
)
def test_scores_known_preserve_scale_and_precision(
    comparator: NameComparator, query: str, candidate: str, expected: float
) -> None:
    score = comparator.similarity(query, candidate)

    assert isinstance(score, float)
    assert isfinite(score)
    assert score == pytest.approx(expected)


def test_normalization_belongs_on_domain(comparator: NameComparator) -> None:
    assert comparator.similarity("AXA", "axa") == 0.0
    assert (
        comparator.similarity(
            normalize("SAMPLE, R\u00e1dono M\u00edzafe"), normalize("Mizafe Radono Sample")
        )
        == 100.0
    )
