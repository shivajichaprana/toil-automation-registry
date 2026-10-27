#!/usr/bin/env python3
"""Validate the toil inventory and report the annual load it implies.

The script does three separable jobs, in order:

1.  **Schema validation.** Every file in ``registry/`` is checked against the
    normative JSON Schema in ``schema/``. This catches shape errors: a missing
    required field, an unknown key, a basis of ``measured`` with no observation
    window behind it.

2.  **Cross-field and cross-file invariants.** A JSON Schema states
    constraints on a value; it cannot say that ``p90`` must not be below
    ``p50``, that the filename must match the ``id`` inside it, or that two
    entries must not claim the same id. Those checks live here, and they are
    the ones that catch a copy-and-edit mistake.

3.  **Normalisation.** The registry stores occurrence rates and per-occurrence
    costs, never a pre-multiplied total, so the annual load is derived on
    read. It is derived twice -- once from the median handling time and once
    from the ninetieth percentile -- and reported as a bracket, because the
    sum of medians is not the median of the sum and a single figure here would
    be precise about something it does not know.

Exit codes: ``0`` clean, ``1`` validation failed, ``2`` could not run.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover - environment problem, not data
    raise SystemExit(
        "jsonschema is required: python3 -m pip install -r requirements-dev.txt"
    ) from exc

LOGGER: Final = logging.getLogger("toil.validate")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA: Final[Path] = REPO_ROOT / "schema" / "toil.schema.json"
DEFAULT_REGISTRY: Final[Path] = REPO_ROOT / "registry"

#: Occurrences per year for each period the schema accepts. A plain 365 is used
#: rather than 365.25: the registry's inputs are nowhere near precise enough for
#: the quarter-day to matter, and a round number is easier to reproduce by hand.
PER_YEAR: Final[dict[str, float]] = {
    "day": 365.0,
    "week": 52.0,
    "month": 12.0,
    "quarter": 4.0,
    "year": 1.0,
}

#: Weight applied to the out-of-hours share of occurrences. A policy constant,
#: not a measurement: it asserts that an occurrence handled at night costs half
#: again what the same occurrence costs during working hours, in recovery and
#: in error rate. It is declared once, here, so that every entry is compared on
#: the same terms and so that a reviewer who disagrees has one number to argue
#: with rather than an assumption buried in a report.
OUT_OF_HOURS_MULTIPLIER: Final[float] = 1.5

#: Defaults the schema declares. ``jsonschema`` does not inject defaults, so the
#: values are repeated here and then checked against the schema at startup --
#: a silent divergence between the two would change every derived figure.
SCHEMA_DEFAULTS: Final[dict[str, Any]] = {
    "responders": 1,
    "context_switch_minutes": 15,
    "out_of_hours_fraction": 0,
}

ERROR: Final = "error"
WARNING: Final = "warning"


@dataclass(frozen=True)
class Finding:
    """One problem with one entry, located precisely enough to fix."""

    level: str
    source: str
    pointer: str
    message: str

    def render(self) -> str:
        where = f"{self.source}:{self.pointer}" if self.pointer else self.source
        return f"{self.level.upper():<7} {where}  {self.message}"


@dataclass(frozen=True)
class AnnualLoad:
    """Load implied by an entry over a year, as a bracket rather than a point."""

    item_id: str
    occurrences: float
    hours_from_p50: float
    hours_from_p90: float

    @property
    def spread_ratio(self) -> float:
        """How much wider the upper end is. A large value means the entry's
        cost is dominated by its tail, which is a different problem from an
        entry that is merely frequent."""
        if self.hours_from_p50 <= 0:
            return float("inf")
        return self.hours_from_p90 / self.hours_from_p50


@dataclass
class Entry:
    """A parsed registry file, kept with its path so findings can cite it."""

    path: Path
    data: dict[str, Any]

    @property
    def item_id(self) -> str:
        value = self.data.get("id")
        return value if isinstance(value, str) else self.path.stem

    @property
    def source(self) -> str:
        return self.path.name


@dataclass
class Report:
    """Everything one run produced."""

    findings: list[Finding] = field(default_factory=list)
    loads: list[AnnualLoad] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == WARNING]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_schema(path: Path) -> dict[str, Any]:
    """Read the normative schema and refuse to continue if it is itself wrong."""
    schema = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    Draft202012Validator.check_schema(schema)
    LOGGER.debug("schema loaded from %s", path)
    return schema


def load_entries(registry: Path) -> tuple[list[Entry], list[Finding]]:
    """Parse every ``*.json`` under ``registry``.

    A file that does not parse yields a finding and is dropped, so one broken
    file does not hide the state of the rest of the inventory.
    """
    entries: list[Entry] = []
    findings: list[Finding] = []
    for path in sorted(registry.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            findings.append(
                Finding(ERROR, path.name, f"line {exc.lineno}", f"not valid JSON: {exc.msg}")
            )
            continue
        if not isinstance(data, dict):
            findings.append(Finding(ERROR, path.name, "", "top level must be an object"))
            continue
        entries.append(Entry(path=path, data=data))
    LOGGER.debug("loaded %d entries from %s", len(entries), registry)
    return entries, findings


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def check_schema_defaults(schema: dict[str, Any]) -> Iterator[Finding]:
    """Confirm the module's constants still match the schema's declared defaults.

    The derived figures use these values whenever an entry omits the field. If
    the schema's default drifts from the constant, every omitted field starts
    being costed differently from how the schema documents it, and nothing
    else in the pipeline would notice.
    """
    cost_properties = schema.get("$defs", {}).get("cost", {}).get("properties", {})
    for key, expected in SCHEMA_DEFAULTS.items():
        declared = cost_properties.get(key, {}).get("default")
        if declared is None:
            yield Finding(
                ERROR,
                "schema/toil.schema.json",
                f"$defs.cost.properties.{key}",
                "no default declared, but the validator substitutes "
                f"{expected!r} when an entry omits it",
            )
        elif declared != expected:
            yield Finding(
                ERROR,
                "schema/toil.schema.json",
                f"$defs.cost.properties.{key}",
                f"schema default {declared!r} disagrees with the validator's {expected!r}",
            )


def check_against_schema(entry: Entry, validator: Draft202012Validator) -> Iterator[Finding]:
    """Yield one finding per schema violation, deepest path first."""
    for error in sorted(validator.iter_errors(entry.data), key=lambda e: list(e.absolute_path)):
        pointer = "/".join(str(part) for part in error.absolute_path) or "(root)"
        yield Finding(ERROR, entry.source, pointer, error.message)


def check_identity(entry: Entry) -> Iterator[Finding]:
    """The filename stem is the address of the entry; a mismatch breaks citation."""
    declared = entry.data.get("id")
    if isinstance(declared, str) and declared != entry.path.stem:
        yield Finding(
            ERROR,
            entry.source,
            "id",
            f"id {declared!r} does not match the filename stem {entry.path.stem!r}; "
            "reports cite the id and resolve it to this path",
        )


def check_spreads(entry: Entry) -> Iterator[Finding]:
    """Percentiles must be ordered. JSON Schema cannot compare siblings."""
    handling = entry.data.get("cost", {}).get("handling_minutes", {})
    if (
        _is_number(handling.get("p50"))
        and _is_number(handling.get("p90"))
        and handling["p90"] < handling["p50"]
    ):
        yield Finding(
            ERROR,
            entry.source,
            "cost/handling_minutes",
            f"p90 ({handling['p90']}) is below p50 ({handling['p50']})",
        )
    if (
        _is_number(handling.get("max"))
        and _is_number(handling.get("p90"))
        and handling["max"] < handling["p90"]
    ):
        yield Finding(
            ERROR,
            entry.source,
            "cost/handling_minutes",
            f"max ({handling['max']}) is below p90 ({handling['p90']})",
        )

    build = entry.data.get("automation", {}).get("build_hours", {})
    if (
        _is_number(build.get("p50"))
        and _is_number(build.get("p90"))
        and build["p90"] < build["p50"]
    ):
        yield Finding(
            ERROR,
            entry.source,
            "automation/build_hours",
            f"p90 ({build['p90']}) is below p50 ({build['p50']})",
        )


def check_qualities(entry: Entry) -> Iterator[Finding]:
    """Enforce what membership of this inventory means.

    Work that is not manual and repetitive is not toil. Filing it here inflates
    the inventory and makes every aggregate drawn from it less useful, so it is
    rejected rather than warned about.
    """
    qualities = entry.data.get("qualities")
    if not isinstance(qualities, dict):
        return
    for quality in ("manual", "repetitive"):
        if qualities.get(quality) is False:
            yield Finding(
                ERROR,
                entry.source,
                f"qualities/{quality}",
                f"an inventory item must be {quality}; work that is not belongs in an "
                "incident record or a project backlog, not in the toil inventory",
            )

    state = entry.data.get("automation", {}).get("state")
    if qualities.get("automatable") is False and state not in {
        None,
        "not-assessed",
        "wont-automate",
    }:
        yield Finding(
            ERROR,
            entry.source,
            "automation/state",
            f"state {state!r} claims automation work on an item marked not automatable; "
            "either the quality is wrong or the state is",
        )

    if entry.data.get("trigger") == "scheduled" and qualities.get("interrupt_driven") is True:
        yield Finding(
            WARNING,
            entry.source,
            "qualities/interrupt_driven",
            "a scheduled obligation is marked interrupt-driven; if occurrences really do "
            "arrive unscheduled, the trigger is probably not 'scheduled'",
        )


def check_automation(entry: Entry) -> Iterator[Finding]:
    """Residual load and build effort must stay internally coherent."""
    automation = entry.data.get("automation")
    if not isinstance(automation, dict):
        return
    residual = automation.get("residual_fraction")
    if automation.get("state") == "automated" and residual == 1:
        yield Finding(
            ERROR,
            entry.source,
            "automation/residual_fraction",
            "an automated item that retains all of its load has not been automated",
        )
    if _is_number(residual) and residual == 0:
        yield Finding(
            WARNING,
            entry.source,
            "automation/residual_fraction",
            "a residual of zero claims the automation needs no exception handling, no "
            "review of its runs and no maintenance; state a non-zero figure or expect "
            "the payback estimate to be challenged",
        )


def check_evidence(entry: Entry) -> Iterator[Finding]:
    """Observation windows must run forwards and must not run into the future."""
    today = date.today()
    for index, item in enumerate(entry.data.get("evidence", []) or []):
        if not isinstance(item, dict):
            continue
        start = _parse_date(item.get("observed_from"))
        end = _parse_date(item.get("observed_to"))
        pointer = f"evidence/{index}"
        if start and end and end < start:
            yield Finding(
                ERROR, entry.source, pointer, f"observed_to {end} precedes observed_from {start}"
            )
        if end and end > today:
            yield Finding(ERROR, entry.source, pointer, f"observed_to {end} is in the future")


def check_review(entry: Entry) -> Iterator[Finding]:
    """An entry past its own review cadence is an estimate wearing old labels."""
    review = entry.data.get("review")
    if not isinstance(review, dict):
        return
    last = _parse_date(review.get("last_reviewed"))
    cadence = review.get("cadence_days")
    if last is None:
        return
    today = date.today()
    if last > today:
        yield Finding(
            ERROR, entry.source, "review/last_reviewed", f"last_reviewed {last} is in the future"
        )
        return
    if isinstance(cadence, int):
        elapsed = (today - last).days
        if elapsed > cadence:
            yield Finding(
                WARNING,
                entry.source,
                "review/last_reviewed",
                f"reviewed {elapsed} days ago against a cadence of {cadence}; "
                "its basis labels no longer describe what is in it",
            )


def check_uniqueness(entries: Sequence[Entry]) -> Iterator[Finding]:
    """Two files claiming one id make every citation of that id ambiguous."""
    seen: dict[str, Path] = {}
    for entry in entries:
        declared = entry.data.get("id")
        if not isinstance(declared, str):
            continue
        if declared in seen:
            yield Finding(
                ERROR,
                entry.source,
                "id",
                f"id {declared!r} is already claimed by {seen[declared].name}",
            )
        else:
            seen[declared] = entry.path


ENTRY_CHECKS: Final = (
    check_identity,
    check_spreads,
    check_qualities,
    check_automation,
    check_evidence,
    check_review,
)


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def annual_load(entry: Entry) -> AnnualLoad | None:
    """Derive the annual engineer-hours an entry implies, as a bracket.

    The arithmetic, stated once so that it can be checked by hand:

    ``occurrences  = count * periods_per_year``

    ``per_occurrence_minutes(h) = (h + context_switch) * responders``

    ``annual_minutes(h) = occurrences * per_occurrence_minutes(h) * ooh_weight``

    where ``ooh_weight = 1 + out_of_hours_fraction * (multiplier - 1)``.

    The context switch is charged per responder per occurrence, not per minute:
    that is the whole reason a frequent, short item can cost more than a rare,
    long one with the same total handling time.

    Returns ``None`` when the entry is too broken to normalise; the schema
    findings will already say why.
    """
    frequency = entry.data.get("frequency")
    cost = entry.data.get("cost")
    if not isinstance(frequency, dict) or not isinstance(cost, dict):
        return None

    period = frequency.get("per")
    count = frequency.get("count")
    handling = cost.get("handling_minutes")
    if period not in PER_YEAR or not _is_number(count) or not isinstance(handling, dict):
        return None
    if not (_is_number(handling.get("p50")) and _is_number(handling.get("p90"))):
        return None

    occurrences = float(cast(float, count)) * PER_YEAR[period]
    responders = int(cost.get("responders", SCHEMA_DEFAULTS["responders"]))
    switch = float(cost.get("context_switch_minutes", SCHEMA_DEFAULTS["context_switch_minutes"]))
    out_of_hours = float(
        cost.get("out_of_hours_fraction", SCHEMA_DEFAULTS["out_of_hours_fraction"])
    )
    weight = 1.0 + out_of_hours * (OUT_OF_HOURS_MULTIPLIER - 1.0)

    def hours(handling_minutes: float) -> float:
        per_occurrence = (handling_minutes + switch) * responders
        return occurrences * per_occurrence * weight / 60.0

    return AnnualLoad(
        item_id=entry.item_id,
        occurrences=occurrences,
        hours_from_p50=hours(float(handling["p50"])),
        hours_from_p90=hours(float(handling["p90"])),
    )


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


def validate(registry: Path, schema_path: Path) -> Report:
    """Run every check over the whole registry and return what was found."""
    schema = load_schema(schema_path)
    validator = Draft202012Validator(schema)
    report = Report()

    report.findings.extend(check_schema_defaults(schema))

    entries, load_findings = load_entries(registry)
    report.findings.extend(load_findings)
    if not entries:
        report.findings.append(Finding(ERROR, str(registry), "", "no registry entries found"))
        return report

    for entry in entries:
        report.findings.extend(check_against_schema(entry, validator))
        for check in ENTRY_CHECKS:
            report.findings.extend(check(entry))

    report.findings.extend(check_uniqueness(entries))

    for entry in entries:
        load = annual_load(entry)
        if load is not None:
            report.loads.append(load)

    LOGGER.info(
        "checked %d entries: %d errors, %d warnings",
        len(entries),
        len(report.errors),
        len(report.warnings),
    )
    return report


def render_summary(loads: Sequence[AnnualLoad]) -> str:
    """Render the derived annual load, widest bracket first."""
    if not loads:
        return "No entries could be normalised."
    ordered = sorted(loads, key=lambda load: load.hours_from_p90, reverse=True)
    width = max(len(load.item_id) for load in ordered)
    lines = [
        f"{'item'.ljust(width)}  {'occ/yr':>8}  {'hours/yr (p50)':>14}  {'hours/yr (p90)':>14}",
        f"{'-' * width}  {'-' * 8}  {'-' * 14}  {'-' * 14}",
    ]
    for load in ordered:
        lines.append(
            f"{load.item_id.ljust(width)}  {load.occurrences:8.1f}  "
            f"{load.hours_from_p50:14.1f}  {load.hours_from_p90:14.1f}"
        )
    total_p50 = sum(load.hours_from_p50 for load in loads)
    total_p90 = sum(load.hours_from_p90 for load in loads)
    lines.append(f"{'-' * width}  {'-' * 8}  {'-' * 14}  {'-' * 14}")
    lines.append(
        f"{'total'.ljust(width)}  {sum(load.occurrences for load in loads):8.1f}  "
        f"{total_p50:14.1f}  {total_p90:14.1f}"
    )
    lines.append("")
    lines.append(
        "The two hour columns bracket the annual load: the left is derived from median "
        "handling time, the right from the ninetieth percentile. Neither is the answer. "
        "A reviewer quoting a single figure should say which end they took and why."
    )
    return "\n".join(lines)


def render_json(report: Report) -> str:
    """Machine-readable output for the pipeline to gate on."""
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "constants": {
            "out_of_hours_multiplier": OUT_OF_HOURS_MULTIPLIER,
            "periods_per_year": PER_YEAR,
            "assumed_defaults": SCHEMA_DEFAULTS,
        },
        "findings": [
            {
                "level": finding.level,
                "source": finding.source,
                "pointer": finding.pointer,
                "message": finding.message,
            }
            for finding in report.findings
        ],
        "annual_load": [
            {
                "id": load.item_id,
                "occurrences_per_year": round(load.occurrences, 3),
                "hours_per_year_from_p50": round(load.hours_from_p50, 2),
                "hours_per_year_from_p90": round(load.hours_from_p90, 2),
                "spread_ratio": _finite_or_none(load.spread_ratio),
            }
            for load in report.loads
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def _finite_or_none(value: float) -> float | None:
    """Keep the JSON output standards-compliant.

    ``json.dumps`` writes a non-finite float as the bare token ``Infinity``,
    which most parsers reject. A ratio that is not finite is reported as null
    rather than as a token the pipeline cannot read back.
    """
    if not math.isfinite(value):
        return None
    return round(value, 3)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _render_findings(findings: Iterable[Finding]) -> str:
    return "\n".join(finding.render() for finding in findings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the toil inventory and report the annual load it implies.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="directory of registry entries (default: %(default)s)",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="normative schema (default: %(default)s)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="also print the derived annual load per entry",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit findings and derived load as JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log what is being checked",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    if not args.schema.is_file():
        print(f"schema not found: {args.schema}")
        return 2
    if not args.registry.is_dir():
        print(f"registry directory not found: {args.registry}")
        return 2

    try:
        report = validate(args.registry, args.schema)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not run: {exc}")
        return 2

    if args.as_json:
        print(render_json(report))
    else:
        if report.findings:
            print(_render_findings(report.findings))
            print()
        verdict = "FAILED" if report.errors else "passed"
        print(
            f"Registry {verdict}: {len(report.loads)} entries normalised, "
            f"{len(report.errors)} errors, {len(report.warnings)} warnings."
        )
        if args.summary:
            print()
            print(render_summary(report.loads))

    if report.errors:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
