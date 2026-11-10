"""Exercise payback and prioritisation with small, hand-checkable portfolios."""

import math
from datetime import date
from pathlib import Path

import pytest

from ranking.payback import Exclusion, Payback, Portfolio, assess
from ranking.score import rank_ranges, score_candidates, select
from tools.validate_registry import AnnualLoad, Entry


def candidate(name, *, gross, build, residual=0.0, basis="measured"):
    entry = Entry(
        Path(f"{name}.json"),
        {
            "id": name,
            "frequency": {"basis": basis},
            "cost": {"basis": basis},
            "automation": {
                "state": "candidate",
                "build_hours": {"p50": build[0], "p90": build[1]},
                "residual_fraction": residual,
            },
        },
    )
    return entry, AnnualLoad(name, 12, gross[0], gross[1])


def shortlist(*candidates):
    paybacks = tuple(assess(entry, load, 36) for entry, load in candidates)
    assert all(isinstance(payback, Payback) for payback in paybacks)
    portfolio = Portfolio(paybacks, (), (), 36, tuple(entry for entry, _ in candidates))
    return score_candidates(portfolio, date(2025, 1, 1))


def test_payback_applies_residual_and_retains_both_corners():
    entry, load = candidate("triage", gross=(24, 48), build=(18, 42), residual=0.25)
    result = assess(entry, load, 24)

    assert isinstance(result, Payback)
    assert (result.saving.net_from_p50, result.saving.net_from_p90) == (18, 36)
    assert result.months_best == pytest.approx(6)
    assert result.months_worst == pytest.approx(28)
    assert result.months_conventional == pytest.approx(12)
    assert result.verdict == "marginal"


@pytest.mark.parametrize(
    ("build", "residual", "expected"),
    [
        ((12, 24), 0.0, "robust"),
        ((50, 60), 0.0, "outside-horizon"),
        ((0, 0), 1.0, "no-saving"),
    ],
)
def test_payback_verdicts_at_horizon_boundary_and_zero_savings(build, residual, expected):
    entry, load = candidate("triage", gross=(12, 24), build=build, residual=residual)
    result = assess(entry, load, 24)

    assert isinstance(result, Payback)
    assert result.verdict == expected
    if expected == "robust":
        assert result.months_worst == 24
    if expected == "no-saving":
        assert math.isinf(result.months_best)


def test_ineligible_toil_still_contributes_to_inventory_totals():
    entry, load = candidate("accepted-task", gross=(12, 24), build=(2, 3))
    entry.data["qualities"] = {"automatable": False}
    exclusion = assess(entry, load, 36)

    assert isinstance(exclusion, Exclusion)
    assert exclusion.kind == "not-automatable"
    portfolio = Portfolio((), (exclusion,), (), 36)
    assert portfolio.total_annual_hours_p50 == 12
    assert portfolio.total_annual_hours_p90 == 24
    assert portfolio.removable_annual_hours == 0


def test_confidence_changes_order_and_equal_scores_use_stable_tie_break():
    items = shortlist(
        candidate("a-estimate", gross=(12, 24), build=(3, 5), basis="estimated"),
        candidate("c-measured", gross=(12, 24), build=(3, 5)),
        candidate("b-measured", gross=(12, 24), build=(3, 5)),
    )

    assert [item.item_id for item in items] == ["b-measured", "c-measured", "a-estimate"]
    assert items[0].raw_score == items[2].raw_score
    assert items[0].score > items[2].score
    ranges = rank_ranges(items, jitter=0.2)
    assert [(item.best, item.nominal, item.worst) for item in ranges] == [
        (1, 1, 1),
        (2, 2, 2),
        (3, 3, 3),
    ]


def test_selection_reports_the_exact_gap_and_excludes_marginal_candidate():
    items = shortlist(
        candidate("high-ratio", gross=(18, 18), build=(6, 6)),
        candidate("first-small", gross=(12, 12), build=(5, 5)),
        candidate("second-small", gross=(12, 12), build=(5, 5)),
        candidate("uncertain", gross=(1, 100), build=(1, 5)),
    )
    result = select(items, budget_hours=10)

    assert result.chosen == ("high-ratio",)
    assert result.build_hours == 6
    assert result.annual_hours_saved == 18
    assert result.exact_search_ran
    assert result.best_annual_hours_saved == 24
    assert result.skipped_for_verdict == ("uncertain",)
