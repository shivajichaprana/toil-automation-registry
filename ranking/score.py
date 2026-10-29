#!/usr/bin/env python3
"""Turn payback arithmetic into a ranked, budget-aware automation shortlist.

Run from the repository root::

    python3 -m ranking.score
    python3 -m ranking.score --budget-hours 120
    python3 -m ranking.score --json

A weighted score is the usual way to rank toil, and on its own it is close to
indefensible. Three faults, each addressed here rather than hidden:

**The weights are never written down.** They are here, in ``WEIGHTS``, they
sum to one, and the sum is asserted at import rather than trusted. Each
component is normalised to the same ``[0, 1]`` range with its direction
stated, so no component dominates merely by having the widest units.

**A weak basis is allowed to add to the score.** An entry whose frequency and
handling time were guessed can outrank a measured one on the strength of its
guesses. Confidence is therefore a *multiplier* and not a component: it can
only shrink a score, never contribute to it, and the remedy a report prints
for a de-ranked entry is to measure it rather than to argue about it.

**Nobody checks whether the order survives the weights.** It frequently does
not. Every weight is swept up and down by a declared jitter, the order is
recomputed for each of the resulting combinations, and each entry is published
with the range of ranks it occupied. An entry whose rank moves across half the
list is not ranked third; it is ranked somewhere between second and seventh,
and a shortlist should say so.

One further departure from the usual tool: a ranking is not a plan. Build
capacity is finite, so the shortlist is also selected against a budget, by
descending return per build hour -- the order that is optimal when effort can
be split and near-optimal when it cannot. The gap to the best achievable
selection is measured rather than assumed away.

Exit codes: ``0`` clean, ``1`` a finding at error level, ``2`` could not run.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

try:
    from collectors.base import Basis, weakest
    from ranking.payback import (
        DEFAULT_HORIZON_MONTHS,
        DEFAULT_REGISTRY,
        Payback,
        Portfolio,
        assess_registry,
    )
    from tools.validate_registry import ERROR, WARNING, Finding
except ImportError as exc:  # pragma: no cover - invocation problem, not data
    raise SystemExit(
        "ranking must be run as a module from the repository root: python3 -m ranking.score --help"
    ) from exc

LOGGER: Final = logging.getLogger("toil.rank")

#: What the ranking is for, stated as weights that sum to one.
#:
#: ``load``
#:     Net annual hours the automation would remove. The reason the inventory
#:     exists, and the only component measured in the inventory's own units.
#: ``efficiency``
#:     Hours returned per hour invested over the horizon. The component that
#:     makes the list a plan rather than a wish: a large saving behind an
#:     enormous build is worth less than a small saving behind a trivial one.
#: ``interruption``
#:     Load that arrives unbidden. Hours charged at 03:00 and hours charged at
#:     15:00 are the same hours and not the same cost; the annualiser already
#:     weights the out-of-hours share, and this component says that being
#:     interrupted at all is itself the thing worth removing.
#: ``exposure``
#:     Privileged access and blocked delivery. Both widen the cost beyond the
#:     responder's minutes -- one adds an audit obligation and narrows who can
#:     absorb the work, the other stops somebody else's.
#: ``tail``
#:     How far the ninetieth percentile sits above the median. An entry whose
#:     cost is concentrated in its bad occurrences is a different problem from
#:     one that is merely frequent, and it is the one that burns a shift.
WEIGHTS: Final[dict[str, float]] = {
    "load": 0.35,
    "efficiency": 0.30,
    "interruption": 0.15,
    "exposure": 0.10,
    "tail": 0.10,
}

#: The weights are a policy claim and a wrong sum would silently rescale every
#: score, so the relationship is asserted rather than commented.
_WEIGHT_SUM_TOLERANCE: Final[float] = 1e-9
if abs(sum(WEIGHTS.values()) - 1.0) > _WEIGHT_SUM_TOLERANCE:  # pragma: no cover - import guard
    raise SystemExit(f"ranking weights must sum to 1.0, got {sum(WEIGHTS.values())}")

#: Confidence multipliers by the weaker of the frequency and cost bases. These
#: shrink a score; none of them can raise one. A measured entry is taken at
#: face value, a sampled one loses a sixth, and a wholly estimated one loses
#: two fifths -- enough to move it behind a measured entry of similar size,
#: which is the intent, and not enough to drop it off a shortlist whose first
#: recommendation would then be to go and measure it.
CONFIDENCE: Final[dict[Basis, float]] = {
    "measured": 1.0,
    "sampled": 0.85,
    "estimated": 0.6,
}

#: Return per build hour treated as the top of the efficiency scale. Above it
#: two candidates are equal on this component and the load component separates
#: them. Stated, because a ratio has no natural ceiling and a linear
#: normalisation against the largest observed ratio would let one freakish
#: entry compress every other candidate into the bottom of the range.
REFERENCE_RETURN: Final[float] = 5.0

#: Tail ratio treated as the top of the tail scale: a p90 four times the p50.
REFERENCE_TAIL: Final[float] = 4.0

#: Fraction by which each weight is swept up and down when the order's
#: stability is tested. Twenty-five per cent is a disagreement a reviewer could
#: plausibly have; the sweep is a full product over all weights, so it covers
#: every combination of those disagreements rather than one at a time.
DEFAULT_JITTER: Final[float] = 0.25

#: Candidates above which the exact budget selection is not attempted. The
#: exact search is a subset enumeration, so the cost doubles per candidate;
#: past this point only the greedy order is reported, and the report says that
#: the gap is unmeasured rather than zero.
EXACT_SELECTION_MAX_ITEMS: Final[int] = 18

#: Verdicts that may enter a budgeted selection. A candidate whose pessimistic
#: corner does not repay inside the horizon can still be ranked -- the ranking
#: is a statement about the load -- but committing budget to it is a different
#: decision and is not made silently.
SELECTABLE_VERDICTS: Final[frozenset[str]] = frozenset({"robust"})


@dataclass(frozen=True)
class Components:
    """One candidate's normalised components, every one in ``[0, 1]``."""

    load: float
    efficiency: float
    interruption: float
    exposure: float
    tail: float

    def as_dict(self) -> dict[str, float]:
        return {
            "load": round(self.load, 4),
            "efficiency": round(self.efficiency, 4),
            "interruption": round(self.interruption, 4),
            "exposure": round(self.exposure, 4),
            "tail": round(self.tail, 4),
        }

    def weighted(self, weights: dict[str, float]) -> float:
        return (
            weights["load"] * self.load
            + weights["efficiency"] * self.efficiency
            + weights["interruption"] * self.interruption
            + weights["exposure"] * self.exposure
            + weights["tail"] * self.tail
        )


@dataclass(frozen=True)
class Scored:
    """A candidate with its score, its confidence, and how firm its rank is."""

    payback: Payback
    components: Components
    basis: Basis
    confidence: float
    raw_score: float
    stale: bool

    @property
    def item_id(self) -> str:
        return self.payback.item_id

    @property
    def score(self) -> float:
        """The published score: weighted components, shrunk by confidence."""
        return self.raw_score * self.confidence

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "score": round(self.score, 4),
            "score_before_confidence": round(self.raw_score, 4),
            "basis": self.basis,
            "confidence": self.confidence,
            "components": self.components.as_dict(),
            "stale_review": self.stale,
            "payback": self.payback.as_dict(),
        }


@dataclass(frozen=True)
class RankRange:
    """Where one candidate sat across every weighting that was tried."""

    item_id: str
    nominal: int
    best: int
    worst: int
    variants: int

    @property
    def firm(self) -> bool:
        """True when no weighting in the sweep moved the candidate at all.

        Firm is not the same as well separated. A candidate can hold its rank
        under every weighting and still sit a thousandth of a point above the
        next one, because two candidates that are close in the same direction
        on every component are ordered identically by every weighting. The
        score gap is reported separately for exactly that reason.
        """
        return self.best == self.worst

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "rank": self.nominal,
            "rank_best": self.best,
            "rank_worst": self.worst,
            "firm": self.firm,
            "variants": self.variants,
        }


@dataclass(frozen=True)
class Selection:
    """A shortlist that fits a build budget, and what it cost to be greedy."""

    budget_hours: float
    chosen: tuple[str, ...]
    build_hours: float
    annual_hours_saved: float
    best_annual_hours_saved: float | None
    exact_search_ran: bool
    skipped_for_verdict: tuple[str, ...]

    @property
    def greedy_gap(self) -> float | None:
        """Annual hours the greedy order left on the table, where known."""
        if self.best_annual_hours_saved is None:
            return None
        return self.best_annual_hours_saved - self.annual_hours_saved

    def as_dict(self) -> dict[str, Any]:
        gap = self.greedy_gap
        return {
            "budget_hours": self.budget_hours,
            "chosen": list(self.chosen),
            "build_hours": round(self.build_hours, 1),
            "annual_hours_saved": round(self.annual_hours_saved, 2),
            "best_annual_hours_saved": (
                None
                if self.best_annual_hours_saved is None
                else round(self.best_annual_hours_saved, 2)
            ),
            "greedy_gap_annual_hours": None if gap is None else round(gap, 2),
            "exact_search_ran": self.exact_search_ran,
            "skipped_for_verdict": list(self.skipped_for_verdict),
        }


@dataclass(frozen=True)
class Ranking:
    """Everything one run produced, in the order a review reads it."""

    scored: tuple[Scored, ...]
    ranges: tuple[RankRange, ...]
    portfolio: Portfolio
    selection: Selection | None
    findings: tuple[Finding, ...]
    jitter: float

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == WARNING)

    def range_for(self, item_id: str) -> RankRange | None:
        return next((r for r in self.ranges if r.item_id == item_id), None)


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _flag(block: dict[str, Any], key: str) -> float:
    return 1.0 if block.get(key) is True else 0.0


def _number(block: dict[str, Any], key: str, default: float) -> float:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _block(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def entry_basis(data: dict[str, Any]) -> Basis:
    """Weaker of the frequency and cost bases.

    Resolved with the collectors' own ordering rather than a second one here:
    a figure assembled from a measured count and an estimated handling time is
    an estimate, and there is one definition of that in the repository.
    """
    bases: list[Basis] = []
    for section in ("frequency", "cost"):
        value = _block(data, section).get("basis")
        if value in CONFIDENCE:
            bases.append(value)
    return weakest(bases)


def tail_ratio(payback: Payback) -> float:
    """How far the upper end of the load sits above the lower end."""
    lower = payback.saving.gross_from_p50
    if lower <= 0.0:
        return REFERENCE_TAIL
    return payback.saving.gross_from_p90 / lower


def components_for(payback: Payback, data: dict[str, Any], max_net_hours: float) -> Components:
    """Normalise one candidate's components against the field it competes in.

    ``load`` is relative to the largest candidate in *this* registry, which
    makes the component -- and so the score -- meaningful only within one
    inventory. That is deliberate: the question a shortlist answers is which of
    these to do first, and a score comparable across unrelated inventories
    would need a unit of load that nobody has.
    """
    cost = _block(data, "cost")
    qualities = _block(data, "qualities")

    load = 0.0 if max_net_hours <= 0.0 else _clamp(payback.saving.net_from_p50 / max_net_hours)

    efficiency_ratio = payback.return_per_build_hour_worst
    efficiency = (
        1.0 if efficiency_ratio == float("inf") else _clamp(efficiency_ratio / REFERENCE_RETURN)
    )

    interruption = (
        _flag(qualities, "interrupt_driven") + _clamp(_number(cost, "out_of_hours_fraction", 0.0))
    ) / 2.0
    exposure = (_flag(cost, "requires_privileged_access") + _flag(cost, "blocks_delivery")) / 2.0
    tail = _clamp((tail_ratio(payback) - 1.0) / (REFERENCE_TAIL - 1.0))

    return Components(
        load=load,
        efficiency=efficiency,
        interruption=interruption,
        exposure=exposure,
        tail=tail,
    )


def is_stale(data: dict[str, Any], today: date) -> bool:
    """True when the entry is past its own review cadence.

    Reported beside the score and deliberately *not* folded into it. A stale
    entry's numbers may be wrong in either direction, so shrinking its score
    would assume they are overstated -- and the entry most likely to have grown
    since it was last looked at is the one whose score would then be cut.
    """
    review = _block(data, "review")
    last = review.get("last_reviewed")
    cadence = review.get("cadence_days")
    if not isinstance(last, str) or not isinstance(cadence, int) or isinstance(cadence, bool):
        return False
    try:
        reviewed = date.fromisoformat(last)
    except ValueError:
        return False
    return (today - reviewed).days > cadence


def score_candidates(portfolio: Portfolio, today: date) -> list[Scored]:
    """Score every open candidate, in descending score with ties broken by id."""
    max_net = max((p.saving.net_from_p50 for p in portfolio.paybacks), default=0.0)
    scored: list[Scored] = []
    for payback in portfolio.paybacks:
        entry = portfolio.entry_for(payback.item_id)
        data: dict[str, Any] = entry.data if entry is not None else {}
        components = components_for(payback, data, max_net)
        basis = entry_basis(data)
        scored.append(
            Scored(
                payback=payback,
                components=components,
                basis=basis,
                confidence=CONFIDENCE[basis],
                raw_score=components.weighted(WEIGHTS),
                stale=is_stale(data, today),
            )
        )
    return order(scored)


def order(scored: Sequence[Scored]) -> list[Scored]:
    """Descending score, ties broken by id.

    The tie-break is declared rather than inherited from whatever order the
    files were read in: two entries with identical derived figures must come
    out in the same order on every run, or the word "ranking" is doing no work.
    """
    return sorted(scored, key=lambda s: (-s.score, s.item_id))


# --------------------------------------------------------------------------- #
# Stability of the order under the weights
# --------------------------------------------------------------------------- #


def weight_variants(jitter: float) -> list[dict[str, float]]:
    """Every combination of each weight swept up, down and left alone.

    Each variant is renormalised to sum to one, so a variant differs from the
    declared weights in the *relative* emphasis between components and not in
    the overall scale -- scaling every weight by the same factor would move no
    ranks at all and would make the sweep look stabler than it is.
    """
    if not 0.0 < jitter < 1.0:
        raise ValueError(f"jitter must be between 0 and 1 exclusive, got {jitter}")
    keys = sorted(WEIGHTS)
    factors = (1.0 - jitter, 1.0, 1.0 + jitter)
    variants: list[dict[str, float]] = []
    for combination in itertools.product(factors, repeat=len(keys)):
        raw = {key: WEIGHTS[key] * factor for key, factor in zip(keys, combination, strict=True)}
        total = sum(raw.values())
        variants.append({key: value / total for key, value in raw.items()})
    return variants


def rank_ranges(scored: Sequence[Scored], jitter: float) -> list[RankRange]:
    """Recompute the order under every weighting and report each rank range."""
    nominal = {item.item_id: index + 1 for index, item in enumerate(order(scored))}
    best: dict[str, int] = {item.item_id: len(scored) for item in scored}
    worst: dict[str, int] = {item.item_id: 1 for item in scored}

    variants = weight_variants(jitter)
    for weights in variants:
        reordered = sorted(
            scored,
            key=lambda s: (-s.components.weighted(weights) * s.confidence, s.item_id),
        )
        for index, item in enumerate(reordered, start=1):
            best[item.item_id] = min(best[item.item_id], index)
            worst[item.item_id] = max(worst[item.item_id], index)

    return [
        RankRange(
            item_id=item.item_id,
            nominal=nominal[item.item_id],
            best=best[item.item_id],
            worst=worst[item.item_id],
            variants=len(variants),
        )
        for item in order(scored)
    ]


# --------------------------------------------------------------------------- #
# Selection against a build budget
# --------------------------------------------------------------------------- #


def _greedy(candidates: Sequence[Scored], budget_hours: float) -> tuple[list[str], float, float]:
    """Fill the budget by descending return per build hour.

    This is the order that is provably optimal when effort can be split
    arbitrarily. It is not optimal for whole automations, because a cheap
    candidate skipped for not fitting may have been the one that let two others
    in; the size of that loss is measured in ``_exact`` rather than waved away.
    """
    ordered = sorted(
        candidates,
        key=lambda s: (-s.payback.return_per_build_hour_worst, s.item_id),
    )
    chosen: list[str] = []
    spent = 0.0
    saved = 0.0
    for item in ordered:
        cost = item.payback.build_p90
        if spent + cost <= budget_hours:
            chosen.append(item.item_id)
            spent += cost
            saved += item.payback.saving.net_from_p50
    return chosen, spent, saved


def _exact(candidates: Sequence[Scored], budget_hours: float) -> float | None:
    """Best annual saving any affordable subset achieves, or ``None`` if unrun.

    A plain subset enumeration: the candidate count in a reviewed inventory is
    small, and an exact answer that can be reproduced by hand is worth more
    here than a dynamic program over rounded hours, which would reintroduce an
    approximation in the one figure whose job is to measure one.
    """
    if len(candidates) > EXACT_SELECTION_MAX_ITEMS:
        return None
    best = 0.0
    for size in range(len(candidates) + 1):
        for subset in itertools.combinations(candidates, size):
            cost = sum(item.payback.build_p90 for item in subset)
            if cost <= budget_hours:
                best = max(best, sum(item.payback.saving.net_from_p50 for item in subset))
    return best


def select(scored: Sequence[Scored], budget_hours: float) -> Selection:
    """Choose the shortlist that fits ``budget_hours`` of build effort.

    Only candidates whose *pessimistic* corner repays inside the horizon are
    eligible. A marginal candidate can be argued for, but it cannot be argued
    for silently by a budget calculation, so it is listed as skipped with its
    verdict rather than dropped.
    """
    if budget_hours < 0.0:
        raise ValueError(f"budget must not be negative, got {budget_hours}")
    eligible = [s for s in scored if s.payback.verdict in SELECTABLE_VERDICTS]
    skipped = tuple(
        sorted(s.item_id for s in scored if s.payback.verdict not in SELECTABLE_VERDICTS)
    )
    chosen, spent, saved = _greedy(eligible, budget_hours)
    best = _exact(eligible, budget_hours)
    return Selection(
        budget_hours=budget_hours,
        chosen=tuple(chosen),
        build_hours=spent,
        annual_hours_saved=saved,
        best_annual_hours_saved=best,
        exact_search_ran=best is not None,
        skipped_for_verdict=skipped,
    )


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

#: Share of the list a candidate's rank range may span before the order is
#: reported as undetermined rather than as a rank.
UNSTABLE_RANGE_SHARE: Final[float] = 0.5

#: Rank down to which an estimated basis is called out. A shortlist's top
#: entries are the ones somebody is about to spend a quarter on.
BASIS_CALLOUT_RANK: Final[int] = 3

#: Score difference below which two adjacent candidates are reported as
#: indistinguishable. Firmness and separation are two different questions and
#: the sweep only answers the first: two candidates that differ by a hair in
#: the same direction on every component are firmly ordered under every
#: weighting, and reporting that as a rank implies a confidence the inputs do
#: not support. The threshold is a hundredth of the score range, which is
#: narrower than the rounding on any figure the score is built from.
NEGLIGIBLE_SCORE_GAP: Final[float] = 0.01


def findings_for(scored: Sequence[Scored], ranges: Sequence[RankRange]) -> list[Finding]:
    """What the order itself reveals, as distinct from what each entry says."""
    out: list[Finding] = []
    count = len(ranges)
    if count == 0:
        return out

    by_id = {item.item_id: item for item in scored}
    span_limit = max(1.0, count * UNSTABLE_RANGE_SHARE)

    for rank_range in ranges:
        span = rank_range.worst - rank_range.best + 1
        if span > span_limit:
            out.append(
                Finding(
                    WARNING,
                    rank_range.item_id,
                    "ranking/weights",
                    f"rank is not determined by the data: it moves between "
                    f"{rank_range.best} and {rank_range.worst} of {count} across "
                    f"{rank_range.variants} weightings, so the nominal rank of "
                    f"{rank_range.nominal} should not be read as an ordering",
                )
            )

    ordered = [by_id[r.item_id] for r in ranges if r.item_id in by_id]
    for first, second in zip(ordered, ordered[1:], strict=False):
        gap = first.score - second.score
        if gap < NEGLIGIBLE_SCORE_GAP:
            out.append(
                Finding(
                    WARNING,
                    f"{first.item_id} / {second.item_id}",
                    "ranking/score",
                    f"scores differ by {gap:.4f}, below the {NEGLIGIBLE_SCORE_GAP} at "
                    "which a difference is meaningful: these two are not ordered by "
                    "the data, and a sweep of the weights cannot show it because they "
                    "are close in the same direction on every component",
                )
            )

    for rank_range in ranges[:BASIS_CALLOUT_RANK]:
        item = by_id.get(rank_range.item_id)
        if item is not None and item.basis == "estimated":
            out.append(
                Finding(
                    WARNING,
                    item.item_id,
                    "frequency/basis",
                    f"ranked {rank_range.nominal} on estimated inputs: the shortlist's "
                    "leading recommendation for this entry is to measure it, not to "
                    "build it",
                )
            )
    return out


def selection_findings(selection: Selection, eligible_count: int) -> list[Finding]:
    """What the budget did to the shortlist."""
    out: list[Finding] = []
    if eligible_count > 0 and not selection.chosen:
        out.append(
            Finding(
                WARNING,
                "selection",
                "ranking/budget",
                f"{selection.budget_hours:.0f} build hours does not fit the cheapest "
                "candidate that repays inside the horizon, so the budget buys no "
                "reduction at all rather than a small one",
            )
        )
    gap = selection.greedy_gap
    if gap is not None and gap > 0.0:
        out.append(
            Finding(
                WARNING,
                "selection",
                "ranking/budget",
                f"taking the candidates in return order leaves {gap:.1f} engineer-hours "
                "a year unsaved against the best affordable combination: the ratio "
                "order is optimal only when build effort can be divided",
            )
        )
    if not selection.exact_search_ran:
        out.append(
            Finding(
                WARNING,
                "selection",
                "ranking/budget",
                f"more than {EXACT_SELECTION_MAX_ITEMS} eligible candidates, so the "
                "best affordable combination was not computed and the gap above the "
                "greedy shortlist is unmeasured rather than zero",
            )
        )
    return out


def rank(
    registry: Path,
    *,
    horizon_months: int = DEFAULT_HORIZON_MONTHS,
    jitter: float = DEFAULT_JITTER,
    budget_hours: float | None = None,
    today: date | None = None,
) -> Ranking:
    """Rank the registry's open automation candidates."""
    portfolio = assess_registry(registry, horizon_months)
    scored = score_candidates(portfolio, today or date.today())
    ranges = rank_ranges(scored, jitter)

    findings = list(portfolio.findings)
    findings.extend(findings_for(scored, ranges))

    selection: Selection | None = None
    if budget_hours is not None:
        selection = select(scored, budget_hours)
        eligible = sum(1 for s in scored if s.payback.verdict in SELECTABLE_VERDICTS)
        findings.extend(selection_findings(selection, eligible))

    variants = ranges[0].variants if ranges else 0
    LOGGER.info("ranked %d candidates over %d weightings", len(scored), variants)
    return Ranking(
        scored=tuple(scored),
        ranges=tuple(ranges),
        portfolio=portfolio,
        selection=selection,
        findings=tuple(findings),
        jitter=jitter,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _rank_cell(rank_range: RankRange | None) -> str:
    if rank_range is None:
        return "?"
    if rank_range.firm:
        return f"{rank_range.nominal}"
    return f"{rank_range.nominal} [{rank_range.best}-{rank_range.worst}]"


def render_summary(ranking: Ranking) -> str:
    """The shortlist, in the order the declared weights produce."""
    portfolio = ranking.portfolio
    lines = [
        "# Automation candidates",
        "",
        f"Weights {_weights_text()} · confidence multiplier applied after · "
        f"stability swept at +/-{ranking.jitter:.0%} over "
        f"{ranking.ranges[0].variants if ranking.ranges else 0} weightings",
        f"Horizon {portfolio.horizon_months} months. A rank shown as "
        "`n [a-b]` moved between a and b under those weightings.",
        "",
        f"| {'rank':<10} | {'id':<42} | {'score':>5} | {'net h/yr':>8} | "
        f"{'build h':>7} | {'payback':>9} | {'ret/h':>5} | basis | verdict |",
        f"|{'-' * 12}|{'-' * 44}|{'-' * 7}|{'-' * 10}|{'-' * 9}|{'-' * 11}|"
        f"{'-' * 7}|-------|---------|",
    ]
    for item in ranking.scored:
        payback = item.payback
        worst = payback.months_worst
        payback_cell = "never" if worst == float("inf") else f"{worst:.1f} mo"
        ret = payback.return_per_build_hour_worst
        ret_cell = "inf" if ret == float("inf") else f"{ret:.2f}"
        flag = " *" if item.stale else ""
        lines.append(
            f"| {_rank_cell(ranking.range_for(item.item_id)):<10} | "
            f"{item.item_id + flag:<42} | {item.score:>5.3f} | "
            f"{payback.saving.net_from_p50:>8.1f} | {payback.build_p90:>7.0f} | "
            f"{payback_cell:>9} | {ret_cell:>5} | {item.basis[:4]:<5} | "
            f"{payback.verdict} |"
        )
    if any(item.stale for item in ranking.scored):
        lines.extend(
            [
                "",
                "`*` past its own review cadence. Staleness is reported and not scored: "
                "the numbers may be wrong in either direction, and shrinking the score "
                "would assume they are overstated.",
            ]
        )

    lines.extend(_render_totals(portfolio))
    if ranking.selection is not None:
        lines.extend(_render_selection(ranking.selection))
    if ranking.portfolio.exclusions:
        lines.extend(["", "## Not ranked", ""])
        for excluded in ranking.portfolio.exclusions:
            lines.append(
                f"- `{excluded.item_id}` — {excluded.annual_hours_p50:.1f} h/yr — "
                f"{excluded.kind}: {excluded.reason}"
            )
    return "\n".join(lines)


def _weights_text() -> str:
    return ", ".join(f"{key} {WEIGHTS[key]:.2f}" for key in sorted(WEIGHTS))


def _render_totals(portfolio: Portfolio) -> list[str]:
    return [
        "",
        "## Load",
        "",
        f"- inventory load: {portfolio.total_annual_hours_p50:.1f} – "
        f"{portfolio.total_annual_hours_p90:.1f} engineer-hours/yr across every entry, "
        "ranked or not",
        f"- removable by the ranked candidates: "
        f"{portfolio.removable_annual_hours:.1f} engineer-hours/yr, net of residual, "
        "at the lower end of the bracket",
        f"- accepted: {portfolio.accepted_annual_hours:.1f} engineer-hours/yr that no "
        "candidate here proposes to remove",
        f"- build outstanding: {portfolio.build_hours_outstanding:.0f} engineer-hours at "
        "the pessimistic estimate",
    ]


def _render_selection(selection: Selection) -> list[str]:
    gap = selection.greedy_gap
    lines = [
        "",
        f"## Shortlist within {selection.budget_hours:.0f} build hours",
        "",
    ]
    if selection.chosen:
        for item_id in selection.chosen:
            lines.append(f"- `{item_id}`")
    else:
        lines.append("- nothing fits")
    lines.extend(
        [
            "",
            f"- spends {selection.build_hours:.0f} of {selection.budget_hours:.0f} build "
            f"hours and removes {selection.annual_hours_saved:.1f} engineer-hours/yr",
        ]
    )
    if gap is None:
        lines.append(
            "- the best affordable combination was not computed, so the loss from "
            "taking candidates in return order is unmeasured"
        )
    elif gap > 0.0:
        lines.append(
            f"- the best affordable combination removes "
            f"{selection.best_annual_hours_saved:.1f} engineer-hours/yr, so return "
            f"order leaves {gap:.1f} unsaved"
        )
    else:
        lines.append("- return order achieves the best affordable combination")
    if selection.skipped_for_verdict:
        lines.append(
            "- not eligible for budget (payback not robust): "
            + ", ".join(f"`{item}`" for item in selection.skipped_for_verdict)
        )
    return lines


def _ranked_payload(item: Scored, rank_range: RankRange | None) -> dict[str, Any]:
    payload = dict(item.as_dict())
    payload["rank"] = None if rank_range is None else rank_range.as_dict()
    return payload


def render_json(ranking: Ranking) -> str:
    portfolio = ranking.portfolio
    payload: dict[str, Any] = {
        "weights": WEIGHTS,
        "confidence_multipliers": CONFIDENCE,
        "reference_return": REFERENCE_RETURN,
        "reference_tail": REFERENCE_TAIL,
        "jitter": ranking.jitter,
        "horizon_months": portfolio.horizon_months,
        "totals": {
            "annual_hours_p50": round(portfolio.total_annual_hours_p50, 2),
            "annual_hours_p90": round(portfolio.total_annual_hours_p90, 2),
            "removable_annual_hours": round(portfolio.removable_annual_hours, 2),
            "accepted_annual_hours": round(portfolio.accepted_annual_hours, 2),
            "build_hours_outstanding": round(portfolio.build_hours_outstanding, 1),
        },
        "ranked": [
            _ranked_payload(item, ranking.range_for(item.item_id)) for item in ranking.scored
        ],
        "not_ranked": [e.as_dict() for e in portfolio.exclusions],
        "selection": None if ranking.selection is None else ranking.selection.as_dict(),
        "findings": [
            {
                "level": f.level,
                "source": f.source,
                "pointer": f.pointer,
                "message": f.message,
            }
            for f in ranking.findings
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m ranking.score",
        description=(
            "Rank the inventory's open automation candidates under declared "
            "weights, report how stable that order is, and fit a shortlist to a "
            "build budget."
        ),
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="directory of registry entries (default: registry/)",
    )
    parser.add_argument(
        "--horizon-months",
        type=int,
        default=DEFAULT_HORIZON_MONTHS,
        help=f"months of automation life assumed (default: {DEFAULT_HORIZON_MONTHS})",
    )
    parser.add_argument(
        "--budget-hours",
        type=float,
        help="build hours available; fits a shortlist and measures the greedy loss",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=DEFAULT_JITTER,
        help=(
            "fraction by which each weight is swept up and down when testing the "
            f"order's stability (default: {DEFAULT_JITTER})"
        ),
    )
    parser.add_argument(
        "--as-of",
        help="ISO date used to judge review staleness (default: today)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--verbose", action="store_true", help="log what was ranked and skipped")
    return parser


def _as_of(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"--as-of must be an ISO date: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        ranking = rank(
            args.registry,
            horizon_months=args.horizon_months,
            jitter=args.jitter,
            budget_hours=args.budget_hours,
            today=_as_of(args.as_of),
        )
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2

    print(render_json(ranking) if args.json else render_summary(ranking))
    for finding in ranking.findings:
        print(finding.render(), file=sys.stderr)

    if ranking.errors:
        return 1
    if args.strict and ranking.warnings:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
