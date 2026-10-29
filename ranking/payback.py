#!/usr/bin/env python3
"""Work out what automating an inventory entry would actually return.

Run from the repository root::

    python3 -m ranking.payback --summary
    python3 -m ranking.payback --item stuck-queue-manual-drain
    python3 -m ranking.payback --json

The module exists because the three figures a toil tracker usually reports are
each wrong in a way that changes the decision.

**Saved hours are not the entry's hours.** Automation does not remove the
load; it removes the share of it that the machine can take, and hands the rest
back as exceptions, review of automated runs, and maintenance of the
automation itself. That share is ``automation.residual_fraction``, and it is
the least reliable number in the entry -- so the saving is computed net of it
and the result is reported with the residual the answer would survive.

**Payback is an interval, not a date.** Build effort is a spread and so is
handling time, which gives four corners rather than one figure. The
conventional single number -- median build over median saving -- is not the
middle of that interval, and quoting it alone hides the corner where the
decision changes: a slow build against a saving at the low end of its spread.
Both ends are reported and the verdict is taken from the *pessimistic* one.

**Payback months do not rank candidates.** Two entries that both pay back in
four months are not equally worth doing when one returns eighty hours a year
and the other eight. Under a finite build budget the comparable quantity is
hours returned per hour invested over a stated horizon, which is what this
module publishes; turning that into an order is ``ranking.score``'s job, under
weights it declares.

Exit codes: ``0`` clean, ``1`` a finding at error level, ``2`` could not run.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

try:
    from tools.validate_registry import (
        ERROR,
        WARNING,
        AnnualLoad,
        Entry,
        Finding,
        annual_load,
        load_entries,
    )
except ImportError as exc:  # pragma: no cover - invocation problem, not data
    raise SystemExit(
        "ranking must be run as a module from the repository root, so that the "
        "annualiser in tools/ is importable: python3 -m ranking.payback --help"
    ) from exc

LOGGER: Final = logging.getLogger("toil.rank")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY: Final[Path] = REPO_ROOT / "registry"

MONTHS_PER_YEAR: Final[float] = 12.0

#: Years of automation life assumed when a build is weighed against a saving.
#: A policy constant, declared rather than discounted: the registry has no
#: field for how long an automation survives, and inventing a discount rate
#: would dress an unknown up as arithmetic. The direction it fails in is
#: stated once, here -- an automation that is rewritten or abandoned before the
#: horizon is reached returns less than this module credits it with, so a
#: candidate whose payback lands near the horizon is a weaker claim than one
#: that pays back in a quarter, even though both are inside it.
DEFAULT_HORIZON_MONTHS: Final[int] = 36

#: Shortest horizon worth asking the question over. Below a year the seasonal
#: entries in the registry (``frequency.seasonal``) have not completed a cycle,
#: so an annualised rate is being compared against a period it was never
#: observed over.
MIN_HORIZON_MONTHS: Final[int] = 12

#: States whose entries are candidates for the ranking. ``not-assessed`` is
#: included deliberately: it is the state that most needs to appear in a review,
#: and the schema does not require a build estimate for it, so it surfaces as
#: unestimated work rather than silently dropping out of the inventory.
OPEN_STATES: Final[frozenset[str]] = frozenset(
    {"not-assessed", "candidate", "planned", "in-progress"}
)

#: The state in which the build estimate in the entry is no longer the cost
#: that remains. The schema records no hours already spent, so the estimate is
#: used as-is and the direction of the error is reported: the remaining build
#: is overstated, the return per hour is understated, and the entry is ranked
#: lower than it deserves rather than higher.
PARTIALLY_BUILT_STATE: Final[str] = "in-progress"

#: Residual headroom below which the decision turns on the single least
#: reliable field in the entry. Not an error: a thin margin is a real answer,
#: and the point of reporting it is that a reviewer can argue about the
#: residual instead of about the verdict.
THIN_RESIDUAL_HEADROOM: Final[float] = 0.05

Verdict = Literal["robust", "marginal", "outside-horizon", "no-saving"]

#: Why an entry is not in the ranking. Each reason keeps the entry's load in
#: the portfolio total: an inventory that drops what it cannot fix reports a
#: smaller problem than it has.
ExclusionKind = Literal["not-automatable", "accepted", "automated", "unestimated"]


@dataclass(frozen=True)
class Saving:
    """Annual engineer-hours an automation would remove, net of the residual.

    Both ends of the handling-time spread are carried through, because the
    spread is what decides whether the pessimistic corner pays back. The
    scaling is linear in the residual share, which assumes the exceptions a
    machine hands back arrive with the same interrupt profile as the work it
    replaced. That assumption fails in one direction: a handed-back exception
    can usually be queued, where the original occurrence could not, so the
    out-of-hours weight and context switch charged against the residual are
    too high and the saving reported here is, if anything, understated.
    """

    item_id: str
    gross_from_p50: float
    gross_from_p90: float
    residual_fraction: float

    @property
    def retained(self) -> float:
        """Share of the original load the automation removes."""
        return 1.0 - self.residual_fraction

    @property
    def net_from_p50(self) -> float:
        return self.gross_from_p50 * self.retained

    @property
    def net_from_p90(self) -> float:
        return self.gross_from_p90 * self.retained


@dataclass(frozen=True)
class Payback:
    """When an automation repays its build, and how wrong the inputs may be."""

    item_id: str
    horizon_months: int
    saving: Saving
    build_p50: float
    build_p90: float
    verdict: Verdict
    build_is_original_estimate: bool

    # --- the interval ---------------------------------------------------- #

    @property
    def months_best(self) -> float:
        """Fast build against the upper end of the saving."""
        return _months(self.build_p50, self.saving.net_from_p90)

    @property
    def months_worst(self) -> float:
        """Slow build against the lower end of the saving. The decision corner."""
        return _months(self.build_p90, self.saving.net_from_p50)

    @property
    def months_conventional(self) -> float:
        """Median build over median saving: the figure most tools print.

        Reported so that a reader can see it is neither end of the interval and
        carries no claim about the corner where the decision changes.
        """
        return _months(self.build_p50, self.saving.net_from_p50)

    # --- the comparable quantity ----------------------------------------- #

    @property
    def horizon_years(self) -> float:
        return self.horizon_months / MONTHS_PER_YEAR

    @property
    def return_per_build_hour_worst(self) -> float:
        """Hours returned over the horizon per hour invested, pessimistic corner.

        A value of exactly 1.0 means the automation breaks even at the horizon
        and nowhere earlier, which is the same thing as not being worth doing.
        """
        return _ratio(self.saving.net_from_p50 * self.horizon_years, self.build_p90)

    @property
    def return_per_build_hour_best(self) -> float:
        return _ratio(self.saving.net_from_p90 * self.horizon_years, self.build_p50)

    @property
    def net_hours_over_horizon_worst(self) -> float:
        return self.saving.net_from_p50 * self.horizon_years - self.build_p90

    # --- how wrong the inputs may be ------------------------------------- #

    @property
    def break_even_residual(self) -> float:
        """Residual share at which the pessimistic corner stops paying back.

        Derived by setting the worst-corner payback equal to the horizon::

            build_p90 * 12 / (gross_p50 * (1 - r)) = horizon_months

        which rearranges to ``r = 1 - 12 * build_p90 / (gross_p50 * horizon)``.
        It is a statement about the *pessimistic* corner, because that is the
        corner the verdict is taken from. A value at or below zero means that
        corner does not repay inside the horizon even if the automation leaves
        nothing behind at all, so no argument about the residual can rescue it
        and the build estimate is the only thing left to argue about.
        """
        if self.saving.gross_from_p50 <= 0.0:
            return 0.0
        horizon_years_of_saving = self.saving.gross_from_p50 * self.horizon_months
        needed = MONTHS_PER_YEAR * self.build_p90 / horizon_years_of_saving
        return 1.0 - needed

    @property
    def residual_headroom(self) -> float:
        """How far the declared residual can be wrong before the verdict flips."""
        return self.break_even_residual - self.saving.residual_fraction

    @property
    def break_even_build_hours(self) -> float:
        """Largest build estimate that still repays inside the horizon.

        The counterpart to the break-even residual: it answers the other
        question a reviewer asks, which is not "is the residual right" but
        "how much build can we afford before this stops being worth doing".
        """
        return self.saving.net_from_p50 * self.horizon_years

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "verdict": self.verdict,
            "horizon_months": self.horizon_months,
            "annual_hours_gross_p50": round(self.saving.gross_from_p50, 2),
            "annual_hours_gross_p90": round(self.saving.gross_from_p90, 2),
            "residual_fraction": self.saving.residual_fraction,
            "annual_hours_net_p50": round(self.saving.net_from_p50, 2),
            "annual_hours_net_p90": round(self.saving.net_from_p90, 2),
            "build_hours_p50": self.build_p50,
            "build_hours_p90": self.build_p90,
            "build_is_original_estimate": self.build_is_original_estimate,
            "payback_months_best": _round_or_none(self.months_best),
            "payback_months_conventional": _round_or_none(self.months_conventional),
            "payback_months_worst": _round_or_none(self.months_worst),
            "return_per_build_hour_worst": _round_or_none(self.return_per_build_hour_worst),
            "return_per_build_hour_best": _round_or_none(self.return_per_build_hour_best),
            "net_hours_over_horizon_worst": round(self.net_hours_over_horizon_worst, 1),
            "break_even_residual": round(self.break_even_residual, 3),
            "residual_headroom": round(self.residual_headroom, 3),
            "break_even_build_hours": round(self.break_even_build_hours, 1),
        }


@dataclass(frozen=True)
class Exclusion:
    """An entry that carries load but is not a candidate, and why.

    The annual hours are kept so that a portfolio total can include them. An
    inventory that reports only the load it intends to remove understates the
    load it has, which is the one number the inventory exists to produce.
    """

    item_id: str
    kind: ExclusionKind
    reason: str
    annual_hours_p50: float
    annual_hours_p90: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "kind": self.kind,
            "reason": self.reason,
            "annual_hours_p50": round(self.annual_hours_p50, 2),
            "annual_hours_p90": round(self.annual_hours_p90, 2),
        }


@dataclass(frozen=True)
class Portfolio:
    """Every entry's assessment, plus the totals a review is read against."""

    paybacks: tuple[Payback, ...]
    exclusions: tuple[Exclusion, ...]
    findings: tuple[Finding, ...]
    horizon_months: int
    #: The entries the assessment was built from, kept so that a consumer can
    #: read the policy fields -- interrupt-driven, privileged access, review
    #: dates -- without opening the registry a second time and risking a
    #: different view of it than the hours were derived from.
    entries: tuple[Entry, ...] = ()

    def entry_for(self, item_id: str) -> Entry | None:
        return next((e for e in self.entries if e.item_id == item_id), None)

    @property
    def horizon_years(self) -> float:
        return self.horizon_months / MONTHS_PER_YEAR

    @property
    def horizon_years_text(self) -> str:
        return f"{self.horizon_years:.1f} years"

    @property
    def total_annual_hours_p50(self) -> float:
        """Load the inventory implies today, candidates and non-candidates alike."""
        return sum(p.saving.gross_from_p50 for p in self.paybacks) + sum(
            e.annual_hours_p50 for e in self.exclusions
        )

    @property
    def total_annual_hours_p90(self) -> float:
        return sum(p.saving.gross_from_p90 for p in self.paybacks) + sum(
            e.annual_hours_p90 for e in self.exclusions
        )

    @property
    def removable_annual_hours(self) -> float:
        """Net annual hours the open candidates could remove, lower end."""
        return sum(p.saving.net_from_p50 for p in self.paybacks)

    @property
    def accepted_annual_hours(self) -> float:
        """Load that no candidate in this registry proposes to remove."""
        return self.total_annual_hours_p50 - self.removable_annual_hours

    @property
    def build_hours_outstanding(self) -> float:
        """Pessimistic build cost of every open candidate."""
        return sum(p.build_p90 for p in self.paybacks)

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == WARNING)


# --------------------------------------------------------------------------- #
# Arithmetic helpers
# --------------------------------------------------------------------------- #


def _months(build_hours: float, net_annual_hours: float) -> float:
    """Months of saving needed to repay ``build_hours``.

    Returns infinity when the saving is not positive, rather than a large
    number: "this never pays back" and "this pays back in nine hundred months"
    are the same answer, and only one of them survives being rounded.
    """
    if net_annual_hours <= 0.0:
        return float("inf")
    return build_hours * MONTHS_PER_YEAR / net_annual_hours


def _ratio(returned_hours: float, invested_hours: float) -> float:
    if invested_hours <= 0.0:
        return float("inf")
    return returned_hours / invested_hours


def _round_or_none(value: float) -> float | None:
    """JSON has no infinity. An unbounded or undefined figure is published as null."""
    if not math.isfinite(value):
        return None
    return round(value, 2)


def _spread(block: object, lower: str, upper: str) -> tuple[float, float] | None:
    """Read an ordered pair out of a schema spread object."""
    if not isinstance(block, dict):
        return None
    low = block.get(lower)
    high = block.get(upper)
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    return float(low), float(high)


# --------------------------------------------------------------------------- #
# Assessment
# --------------------------------------------------------------------------- #


def classify_verdict(payback: Payback) -> Verdict:
    """Three-way verdict taken from the pessimistic corner.

    ``robust``
        The slow build repays itself against the low end of the saving inside
        the horizon. Every corner pays back, so the decision does not depend
        on which estimate turns out to be right.

    ``marginal``
        The optimistic corner repays inside the horizon and the pessimistic one
        does not. This is the band the conventional single figure hides, and
        the only honest report of it names what would have to hold.

    ``outside-horizon``
        Even the optimistic corner does not repay inside the horizon.

    ``no-saving``
        The residual leaves nothing to save.
    """
    if payback.saving.net_from_p50 <= 0.0 and payback.saving.net_from_p90 <= 0.0:
        return "no-saving"
    horizon = float(payback.horizon_months)
    if payback.months_worst <= horizon:
        return "robust"
    if payback.months_best <= horizon:
        return "marginal"
    return "outside-horizon"


def assess(entry: Entry, load: AnnualLoad, horizon_months: int) -> Payback | Exclusion:
    """Assess one entry: a payback if it is an open candidate, else why not."""
    automation = entry.data.get("automation")
    automation = automation if isinstance(automation, dict) else {}
    state = str(automation.get("state", "not-assessed"))
    qualities = entry.data.get("qualities")
    qualities = qualities if isinstance(qualities, dict) else {}

    if qualities.get("automatable") is False:
        return Exclusion(
            entry.item_id,
            "not-automatable",
            "declared not automatable, so its load is accepted rather than removable",
            load.hours_from_p50,
            load.hours_from_p90,
        )
    if state == "wont-automate":
        return Exclusion(
            entry.item_id,
            "accepted",
            "a dated decision to accept the load; the hours remain in the total",
            load.hours_from_p50,
            load.hours_from_p90,
        )
    if state == "automated":
        return Exclusion(
            entry.item_id,
            "automated",
            "already automated; the hours recorded here are the residual that survived",
            load.hours_from_p50,
            load.hours_from_p90,
        )
    if state not in OPEN_STATES:
        return Exclusion(
            entry.item_id,
            "unestimated",
            f"unrecognised automation state {state!r}",
            load.hours_from_p50,
            load.hours_from_p90,
        )

    build = _spread(automation.get("build_hours"), "p50", "p90")
    residual = automation.get("residual_fraction")
    if build is None or not isinstance(residual, (int, float)) or isinstance(residual, bool):
        return Exclusion(
            entry.item_id,
            "unestimated",
            "no build-hours spread or residual share, so no payback can be computed",
            load.hours_from_p50,
            load.hours_from_p90,
        )

    saving = Saving(
        item_id=entry.item_id,
        gross_from_p50=load.hours_from_p50,
        gross_from_p90=load.hours_from_p90,
        residual_fraction=float(residual),
    )
    provisional = Payback(
        item_id=entry.item_id,
        horizon_months=horizon_months,
        saving=saving,
        build_p50=build[0],
        build_p90=build[1],
        verdict="robust",
        build_is_original_estimate=state == PARTIALLY_BUILT_STATE,
    )
    verdict = classify_verdict(provisional)
    return Payback(
        item_id=provisional.item_id,
        horizon_months=horizon_months,
        saving=saving,
        build_p50=provisional.build_p50,
        build_p90=provisional.build_p90,
        verdict=verdict,
        build_is_original_estimate=provisional.build_is_original_estimate,
    )


def findings_for(payback: Payback, source: str) -> list[Finding]:
    """Everything about this payback a reviewer would otherwise have to notice.

    The findings deliberately do not repeat what the validator already says
    about the entry's shape. They say only what the payback arithmetic reveals,
    which is in every case a statement about how the answer could be wrong.
    """
    out: list[Finding] = []
    if payback.verdict == "no-saving":
        out.append(
            Finding(
                ERROR,
                source,
                "automation/residual_fraction",
                "the residual leaves no load to remove, so there is nothing to pay back",
            )
        )
        return out

    if payback.verdict == "marginal":
        out.append(
            Finding(
                WARNING,
                source,
                "automation/build_hours",
                f"pays back in {payback.months_best:.1f} months at the optimistic corner "
                f"and {payback.months_worst:.1f} at the pessimistic one, against a "
                f"{payback.horizon_months}-month horizon: the conventional figure of "
                f"{payback.months_conventional:.1f} months is inside the horizon and does "
                "not establish that the build is worth starting",
            )
        )

    if payback.break_even_residual <= 0.0:
        out.append(
            Finding(
                WARNING,
                source,
                "automation/build_hours",
                "the pessimistic corner does not repay inside the horizon even with "
                "no residual load at all, so the build estimate must fall below "
                f"{payback.break_even_build_hours:.0f} hours before the residual is "
                "worth arguing about",
            )
        )
    elif 0.0 < payback.residual_headroom < THIN_RESIDUAL_HEADROOM:
        out.append(
            Finding(
                WARNING,
                source,
                "automation/residual_fraction",
                f"the verdict turns on the residual: it is declared at "
                f"{payback.saving.residual_fraction:.2f} and flips at "
                f"{payback.break_even_residual:.2f}",
            )
        )

    if payback.build_is_original_estimate:
        out.append(
            Finding(
                WARNING,
                source,
                "automation/build_hours",
                "build already under way, and the registry records no hours spent: the "
                "estimate used here is the original, which overstates what remains and "
                "so ranks the entry lower than it deserves",
            )
        )
    return out


def assess_registry(registry: Path, horizon_months: int) -> Portfolio:
    """Assess every entry in ``registry``."""
    if horizon_months < MIN_HORIZON_MONTHS:
        raise ValueError(
            f"horizon of {horizon_months} months is below the {MIN_HORIZON_MONTHS}-month "
            "minimum: an annualised rate cannot be compared against a period shorter "
            "than the year it was annualised over"
        )

    if not registry.is_dir():
        raise ValueError(f"registry directory does not exist: {registry}")

    entries, findings = load_entries(registry)
    if not entries:
        findings.append(
            Finding(
                ERROR,
                str(registry),
                "",
                "no registry entries found: an empty inventory implies no toil, which "
                "is the one conclusion a reviewer must never reach by accident",
            )
        )

    paybacks: list[Payback] = []
    exclusions: list[Exclusion] = []
    collected = list(findings)

    for entry in entries:
        load = annual_load(entry)
        if load is None:
            collected.append(
                Finding(
                    ERROR,
                    entry.source,
                    "",
                    "cannot be normalised, so it cannot be ranked; run the validator",
                )
            )
            continue
        outcome = assess(entry, load, horizon_months)
        if isinstance(outcome, Payback):
            paybacks.append(outcome)
            collected.extend(findings_for(outcome, entry.source))
        else:
            exclusions.append(outcome)

    LOGGER.info(
        "assessed %d entries: %d open candidates, %d not ranked",
        len(entries),
        len(paybacks),
        len(exclusions),
    )
    return Portfolio(
        paybacks=tuple(sorted(paybacks, key=lambda p: p.item_id)),
        exclusions=tuple(sorted(exclusions, key=lambda e: e.item_id)),
        findings=tuple(collected),
        horizon_months=horizon_months,
        entries=tuple(entries),
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _months_cell(value: float) -> str:
    return "never" if value == float("inf") else f"{value:.1f}"


def render_summary(portfolio: Portfolio) -> str:
    """One row per open candidate, longest pessimistic payback last."""
    lines = [
        f"Horizon: {portfolio.horizon_months} months "
        f"({portfolio.horizon_years_text}) · residual applied per entry",
        "",
        f"{'id':<42} {'net h/yr':>9} {'build h':>9} {'best':>7} {'worst':>7} "
        f"{'ret/h':>7} {'r*':>6} verdict",
        "-" * 108,
    ]
    ordered = sorted(portfolio.paybacks, key=lambda p: (p.months_worst, p.item_id))
    for item in ordered:
        lines.append(
            f"{item.item_id:<42} {item.saving.net_from_p50:>9.1f} "
            f"{item.build_p90:>9.0f} {_months_cell(item.months_best):>7} "
            f"{_months_cell(item.months_worst):>7} "
            f"{_months_cell(item.return_per_build_hour_worst):>7} "
            f"{item.break_even_residual:>6.2f} {item.verdict}"
        )
    if portfolio.exclusions:
        lines.extend(["", "Not ranked (load still counted):"])
        for excluded in portfolio.exclusions:
            lines.append(
                f"  {excluded.item_id:<42} {excluded.annual_hours_p50:>9.1f} h/yr  "
                f"{excluded.kind}: {excluded.reason}"
            )
    lines.extend(
        [
            "",
            f"Inventory load   {portfolio.total_annual_hours_p50:>9.1f} - "
            f"{portfolio.total_annual_hours_p90:.1f} engineer-hours/yr "
            "(every entry, p50 to p90)",
            f"Removable        {portfolio.removable_annual_hours:>9.1f} engineer-hours/yr "
            "by the open candidates, net of residual, at the p50 end",
            f"Accepted         {portfolio.accepted_annual_hours:>9.1f} engineer-hours/yr "
            "that no candidate here proposes to remove",
            f"Build outstanding{portfolio.build_hours_outstanding:>9.0f} engineer-hours "
            "at the pessimistic estimate",
        ]
    )
    return "\n".join(lines)


def render_detail(payback: Payback) -> str:
    """Everything behind one entry's verdict, in the order a reviewer reads it."""
    saving = payback.saving
    partial_note = (
        "  (original estimate; work already under way)"
        if payback.build_is_original_estimate
        else ""
    )
    lines = [
        f"{payback.item_id}  [{payback.verdict}]",
        "",
        f"  load today          {saving.gross_from_p50:.1f} - {saving.gross_from_p90:.1f} "
        "engineer-hours/yr",
        f"  residual share      {saving.residual_fraction:.2f} "
        f"(automation removes {saving.retained:.0%})",
        f"  net saving          {saving.net_from_p50:.1f} - {saving.net_from_p90:.1f} "
        "engineer-hours/yr",
        f"  build estimate      {payback.build_p50:.0f} - {payback.build_p90:.0f} "
        f"engineer-hours{partial_note}",
        "",
        f"  payback best        {_months_cell(payback.months_best)} months "
        "(fast build, upper end of saving)",
        f"  payback conventional{_months_cell(payback.months_conventional):>7} months "
        "(median over median - neither end of the interval)",
        f"  payback worst       {_months_cell(payback.months_worst)} months "
        "(slow build, lower end of saving) <- the decision corner",
        "",
        f"  return per build hour over the horizon: "
        f"{_months_cell(payback.return_per_build_hour_worst)} (worst) to "
        f"{_months_cell(payback.return_per_build_hour_best)} (best)",
        f"  net over horizon at the worst corner:   "
        f"{payback.net_hours_over_horizon_worst:.0f} engineer-hours",
        "",
        f"  residual would have to reach {payback.break_even_residual:.2f} to flip the "
        f"verdict (declared {saving.residual_fraction:.2f}, "
        f"headroom {payback.residual_headroom:+.2f})",
        f"  build would have to exceed   {payback.break_even_build_hours:.0f} engineer-hours "
        "to stop repaying inside the horizon",
    ]
    return "\n".join(lines)


def render_json(portfolio: Portfolio) -> str:
    payload = {
        "horizon_months": portfolio.horizon_months,
        "totals": {
            "annual_hours_p50": round(portfolio.total_annual_hours_p50, 2),
            "annual_hours_p90": round(portfolio.total_annual_hours_p90, 2),
            "removable_annual_hours": round(portfolio.removable_annual_hours, 2),
            "accepted_annual_hours": round(portfolio.accepted_annual_hours, 2),
            "build_hours_outstanding": round(portfolio.build_hours_outstanding, 1),
        },
        "candidates": [p.as_dict() for p in portfolio.paybacks],
        "not_ranked": [e.as_dict() for e in portfolio.exclusions],
        "findings": [
            {
                "level": f.level,
                "source": f.source,
                "pointer": f.pointer,
                "message": f.message,
            }
            for f in portfolio.findings
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m ranking.payback",
        description=(
            "Report what automating each inventory entry would return, as an "
            "interval, with the residual and build estimate the answer survives."
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
        help=(
            f"months of automation life assumed (default: {DEFAULT_HORIZON_MONTHS}, "
            f"minimum: {MIN_HORIZON_MONTHS})"
        ),
    )
    parser.add_argument(
        "--item",
        help="report one entry in full rather than the whole registry",
    )
    parser.add_argument("--summary", action="store_true", help="print the table (default)")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )
    parser.add_argument("--verbose", action="store_true", help="log what was assessed and skipped")
    return parser


def _exit_code(portfolio: Portfolio, *, strict: bool) -> int:
    if portfolio.errors:
        return 1
    if strict and portfolio.warnings:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        portfolio = assess_registry(args.registry, args.horizon_months)
    except (OSError, ValueError) as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2

    if args.item:
        match = next((p for p in portfolio.paybacks if p.item_id == args.item), None)
        if match is None:
            excluded = next((e for e in portfolio.exclusions if e.item_id == args.item), None)
            if excluded is None:
                print(f"cannot run: no entry with id {args.item!r}", file=sys.stderr)
                return 2
            print(f"{excluded.item_id}  [not ranked: {excluded.kind}]")
            print(f"  {excluded.reason}")
            print(f"  load today {excluded.annual_hours_p50:.1f} engineer-hours/yr")
            return _exit_code(portfolio, strict=args.strict)
        print(render_detail(match))
    elif args.json:
        print(render_json(portfolio))
    else:
        print(render_summary(portfolio))

    for finding in portfolio.findings:
        print(finding.render(), file=sys.stderr)
    return _exit_code(portfolio, strict=args.strict)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
