#!/usr/bin/env python3
"""Measure toil from an alert-firing export.

Alert history looks like the cleanest source a toil inventory could ask for --
machine-generated, timestamped, complete -- and counting rows in it produces
the wrong number in two directions at once.

**A firing is not an occurrence.** An alert on a threshold that is being
crossed repeatedly fires, resolves, and fires again; the responder handled one
interruption. Counting firings inflates the occurrence count for exactly the
noisiest rules, which then rank highest as automation candidates on the
strength of their noise rather than their load. Firings on the same rule and
target that arrive within a correlation window of the previous one are
therefore folded into a single occurrence, and both the raw and the folded
counts are reported so the folding is visible.

**An alert nobody touched consumed nobody.** A firing that resolved itself in
ninety seconds without an acknowledgement cost no engineer time. Counting it
inflates the frequency while deflating the median handling time, which moves
the entry's rank in two directions and makes the error hard to see. Such
firings are dropped by default, and the count of what was dropped is reported:
a rule whose firings are mostly self-resolving has an alert-quality problem,
not a toil problem, and the inventory should not absorb it as load.

Handling time within an occurrence is the **union** of its engaged intervals,
not their sum. A flapping alert acknowledged once can produce overlapping
intervals across its firings, and adding those up charges the same minute
twice.

The collector reads a file and holds no credentials.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from collectors.base import (
    LEVEL_NOTE,
    LEVEL_WARNING,
    Basis,
    CollectorError,
    Measurement,
    Note,
    Observation,
    add_common_arguments,
    business_hours_from_args,
    coerce_datetime,
    measure,
    pick,
    read_records,
    run,
    weakest,
    window_from_args,
)

RULE_FIELDS: Final[tuple[str, ...]] = (
    "rule",
    "alert_name",
    "alertname",
    "name",
    "labels.alertname",
)
TARGET_FIELDS: Final[tuple[str, ...]] = (
    "target",
    "instance",
    "resource",
    "service",
    "labels.instance",
)
FIRED_FIELDS: Final[tuple[str, ...]] = ("fired_at", "starts_at", "startsAt", "triggered_at")
ACK_FIELDS: Final[tuple[str, ...]] = ("acknowledged_at", "acked_at", "ack_at", "engaged_at")
RESOLVED_FIELDS: Final[tuple[str, ...]] = ("resolved_at", "ends_at", "endsAt", "cleared_at")
RESPONDER_FIELDS: Final[tuple[str, ...]] = (
    "acknowledged_by",
    "responders",
    "assignees",
    "acked_by",
)
SEVERITY_FIELDS: Final[tuple[str, ...]] = ("severity", "priority", "labels.severity")

#: Firings on one rule and target arriving within this many minutes of the end
#: of the previous firing belong to the same interruption. Chosen as a default
#: rather than derived: it is roughly how long a responder stays with a problem
#: after it appears to clear, and it is the one knob worth tuning per rule.
DEFAULT_CORRELATION_MINUTES: Final[float] = 30.0

#: A firing that resolved itself faster than this, with nobody acknowledging
#: it, is treated as alert noise rather than as work.
DEFAULT_SELF_RESOLVE_MINUTES: Final[float] = 5.0

#: Share of a rule's firings that may be self-resolving before the measurement
#: says so rather than quietly absorbing it.
NOISE_WARNING_FRACTION: Final[float] = 0.3


@dataclass(frozen=True)
class Firing:
    """One alert firing, as the export recorded it."""

    rule: str
    target: str
    fired_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    responders: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule, self.target)

    @property
    def ended_at(self) -> datetime:
        """When this firing stopped being live, for correlation purposes."""
        return self.resolved_at or self.fired_at

    @property
    def self_resolved_within(self) -> float | None:
        """Minutes to self-resolution, or ``None`` if it was acknowledged."""
        if self.acknowledged_at is not None or self.resolved_at is None:
            return None
        return (self.resolved_at - self.fired_at).total_seconds() / 60.0

    def engaged_interval(self) -> tuple[datetime, datetime] | None:
        """The interval a person was on this firing, as far as it is knowable."""
        if self.resolved_at is None:
            return None
        start = self.acknowledged_at or self.fired_at
        if self.resolved_at <= start:
            return None
        return (start, self.resolved_at)


@dataclass(frozen=True)
class Occurrence:
    """One interruption, assembled from the firings that make it up."""

    key: tuple[str, str]
    firings: tuple[Firing, ...]

    @property
    def ref(self) -> str:
        return f"{self.key[0]}@{self.key[1]}:{self.firings[0].fired_at.isoformat()}"

    @property
    def started_at(self) -> datetime:
        return self.firings[0].fired_at

    @property
    def responders(self) -> int:
        names = {name for firing in self.firings for name in firing.responders}
        return max(1, len(names))

    @property
    def acknowledged(self) -> bool:
        return any(firing.acknowledged_at is not None for firing in self.firings)

    def handling_minutes(self) -> float | None:
        """Union of the engaged intervals, in minutes.

        The union rather than the sum: a flapping alert acknowledged once
        produces overlapping intervals across its firings, and summing them
        charges the same minute of somebody's attention more than once.
        """
        intervals = [
            interval
            for interval in (firing.engaged_interval() for firing in self.firings)
            if interval is not None
        ]
        if not intervals:
            return None
        ordered = sorted(intervals)
        total = timedelta()
        current_start, current_end = ordered[0]
        for start, end in ordered[1:]:
            if start > current_end:
                total += current_end - current_start
                current_start, current_end = start, end
            else:
                current_end = max(current_end, end)
        total += current_end - current_start
        minutes = total.total_seconds() / 60.0
        return minutes if minutes > 0 else None

    def handling_basis(self) -> Basis:
        """Acknowledged firings time the work; unacknowledged ones bracket it."""
        return "measured" if self.acknowledged else "estimated"


def parse_firing(record: dict[str, Any], index: int) -> Firing:
    """Read one export record into a firing, refusing ambiguous timestamps."""
    rule = _text(pick(record, RULE_FIELDS)) or f"rule-{index}"
    target = _text(pick(record, TARGET_FIELDS)) or "unspecified-target"
    fired_at = coerce_datetime(pick(record, FIRED_FIELDS), context=f"firing {index} fired_at")
    acknowledged_raw = pick(record, ACK_FIELDS)
    resolved_raw = pick(record, RESOLVED_FIELDS)
    acknowledged_at = (
        coerce_datetime(acknowledged_raw, context=f"firing {index} acknowledged_at")
        if acknowledged_raw is not None
        else None
    )
    resolved_at = (
        coerce_datetime(resolved_raw, context=f"firing {index} resolved_at")
        if resolved_raw is not None
        else None
    )
    if resolved_at is not None and resolved_at < fired_at:
        raise CollectorError(
            f"firing {index} on {rule}: resolved at {resolved_at.isoformat()} before it fired at "
            f"{fired_at.isoformat()}; the export's timestamps disagree"
        )
    if acknowledged_at is not None and acknowledged_at < fired_at:
        raise CollectorError(
            f"firing {index} on {rule}: acknowledged before it fired; the export's timestamps "
            "disagree"
        )
    return Firing(
        rule=rule,
        target=target,
        fired_at=fired_at,
        acknowledged_at=acknowledged_at,
        resolved_at=resolved_at,
        responders=_names(pick(record, RESPONDER_FIELDS)),
    )


def correlate(firings: Sequence[Firing], correlation_minutes: float) -> list[Occurrence]:
    """Fold flapping firings on one rule and target into single occurrences.

    A session window rather than a fixed bucket: a firing joins the occurrence
    in progress when it arrives within ``correlation_minutes`` of the end of
    the previous firing. A fixed hourly bucket would split an interruption that
    straddles the hour and merge two unrelated ones that share it.
    """
    if correlation_minutes < 0:
        raise CollectorError("correlation window cannot be negative")
    gap = timedelta(minutes=correlation_minutes)
    grouped: dict[tuple[str, str], list[Firing]] = {}
    for firing in firings:
        grouped.setdefault(firing.key, []).append(firing)

    occurrences: list[Occurrence] = []
    for key, members in grouped.items():
        ordered = sorted(members, key=lambda item: item.fired_at)
        current: list[Firing] = [ordered[0]]
        boundary = ordered[0].ended_at + gap
        for firing in ordered[1:]:
            if firing.fired_at <= boundary:
                current.append(firing)
            else:
                occurrences.append(Occurrence(key=key, firings=tuple(current)))
                current = [firing]
            boundary = max(boundary, firing.ended_at + gap)
        occurrences.append(Occurrence(key=key, firings=tuple(current)))
    return sorted(occurrences, key=lambda occurrence: occurrence.started_at)


def _text(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        single = _text(value)
        return (single,) if single else ()
    if isinstance(value, list):
        return tuple(name for name in (_text(item) for item in value) if name is not None)
    return ()


def collect(args: argparse.Namespace) -> Measurement:
    """Read the export, fold firings into occurrences, and measure them."""
    window = window_from_args(args)
    business_hours = business_hours_from_args(args)
    records = read_records(args.export, args.records_key)

    kept: list[Firing] = []
    filtered_out = 0
    self_resolved = 0
    for index, record in enumerate(records):
        firing = parse_firing(record, index)
        if args.rule is not None and firing.rule != args.rule:
            filtered_out += 1
            continue
        if args.severity is not None and _text(pick(record, SEVERITY_FIELDS)) != args.severity:
            filtered_out += 1
            continue
        if args.target is not None and firing.target != args.target:
            filtered_out += 1
            continue
        quick = firing.self_resolved_within
        if (
            not args.count_self_resolved
            and quick is not None
            and quick <= float(args.self_resolve_minutes)
        ):
            self_resolved += 1
            continue
        kept.append(firing)

    if not kept:
        raise CollectorError(
            f"no firings survived the filter: {len(records)} read, {filtered_out} filtered out, "
            f"{self_resolved} dropped as self-resolving noise"
        )

    occurrences = correlate(kept, float(args.correlation_minutes))
    observations = [
        Observation(
            ref=occurrence.ref,
            started_at=occurrence.started_at,
            handling_minutes=occurrence.handling_minutes(),
            handling_basis=occurrence.handling_basis(),
            responders=occurrence.responders,
        )
        for occurrence in occurrences
    ]

    notes = _folding_notes(
        raw_firings=len(kept),
        occurrences=occurrences,
        filtered_out=filtered_out,
        self_resolved=self_resolved,
        total_read=len(records),
        correlation_minutes=float(args.correlation_minutes),
        counted_self_resolved=bool(args.count_self_resolved),
    )
    scope = ", ".join(
        part
        for part in (
            f"rule={args.rule}" if args.rule else "",
            f"severity={args.severity}" if args.severity else "",
            f"target={args.target}" if args.target else "",
        )
        if part
    )
    source = f"{args.source} [{scope or 'all rules in export'}]"
    return measure(
        item_id=args.item_id,
        observations=observations,
        window=window,
        source=source,
        business_hours=business_hours,
        complete_enumeration=not args.sampled,
        extra_notes=notes,
    )


def _folding_notes(
    *,
    raw_firings: int,
    occurrences: Sequence[Occurrence],
    filtered_out: int,
    self_resolved: int,
    total_read: int,
    correlation_minutes: float,
    counted_self_resolved: bool,
) -> list[Note]:
    """Report what folding and noise-dropping did, in both directions."""
    notes: list[Note] = [
        Note(
            LEVEL_NOTE,
            f"read {total_read} firings; {filtered_out} outside the selected scope, "
            f"{self_resolved} dropped as self-resolving, {raw_firings} retained",
        ),
        Note(
            LEVEL_NOTE,
            f"{raw_firings} firings folded into {len(occurrences)} occurrences on a "
            f"{correlation_minutes:.0f}-minute correlation window; counting firings instead "
            "would have overstated the occurrence count by "
            f"{raw_firings - len(occurrences)}",
        ),
    ]
    considered = raw_firings + self_resolved
    if self_resolved and considered and self_resolved / considered >= NOISE_WARNING_FRACTION:
        notes.append(
            Note(
                LEVEL_WARNING,
                f"{self_resolved} of {considered} firings in scope resolved themselves without "
                "an acknowledgement; a rule that mostly self-clears is an alert-quality problem "
                "rather than toil, and the inventory should not absorb it as load",
            )
        )
    if counted_self_resolved:
        notes.append(
            Note(
                LEVEL_WARNING,
                "self-resolving firings were counted as occurrences on request; they consumed no "
                "responder time, so they raise the frequency while lowering the median handling "
                "time, and the entry's rank moves for a reason that is not load",
            )
        )
    unacknowledged = sum(1 for occurrence in occurrences if not occurrence.acknowledged)
    if unacknowledged:
        notes.append(
            Note(
                LEVEL_WARNING,
                f"{unacknowledged} of {len(occurrences)} occurrences carried no acknowledgement, "
                "so their handling time is bracketed from the firing instant and includes the "
                "lag before anyone engaged; the cost basis is capped at 'estimated'",
            )
        )
    untimed = sum(1 for occurrence in occurrences if occurrence.handling_minutes() is None)
    if untimed:
        notes.append(
            Note(
                LEVEL_NOTE,
                f"{untimed} occurrence(s) had no resolution timestamp and contributed to the "
                "count but not to the handling spread",
            )
        )
    basis = weakest(occurrence.handling_basis() for occurrence in occurrences)
    notes.append(Note(LEVEL_NOTE, f"weakest contributing handling basis: {basis}"))
    return notes


def build() -> tuple[argparse.ArgumentParser, Callable[[argparse.Namespace], Measurement]]:
    parser = argparse.ArgumentParser(
        description="Measure toil frequency and handling cost from an alert-firing export.",
        epilog="Firings are folded into occurrences and self-resolving noise is dropped, because "
        "a firing is not an interruption and an alert nobody touched cost nobody anything.",
    )
    add_common_arguments(parser)
    parser.add_argument("--rule", help="only firings of this alert rule")
    parser.add_argument("--severity", help="only firings at this severity")
    parser.add_argument("--target", help="only firings against this target or instance")
    parser.add_argument(
        "--correlation-minutes",
        type=float,
        default=DEFAULT_CORRELATION_MINUTES,
        help="firings on one rule and target within this gap are one occurrence "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--self-resolve-minutes",
        type=float,
        default=DEFAULT_SELF_RESOLVE_MINUTES,
        help="unacknowledged firings that cleared within this many minutes are treated as noise "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--count-self-resolved",
        action="store_true",
        help="count self-resolving firings as occurrences anyway; raises the frequency while "
        "lowering the median handling time",
    )
    return parser, collect


def main(argv: Sequence[str] | None = None) -> int:
    return run(build, argv)


if __name__ == "__main__":
    raise SystemExit(main())
