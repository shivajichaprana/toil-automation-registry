#!/usr/bin/env python3
"""Measure toil from a ticket export.

A ticket queue is the usual first source for a toil inventory, and it is also
the source most likely to produce a confident wrong number, because the field
that looks like handling time is not handling time.

**Resolution time is not engaged time.** A ticket opened on Monday and closed
on Thursday did not consume three days of a person. It sat in a queue, waited
on a requester, waited on a change window, and was worked on for twenty
minutes. Costing the inventory on ``resolved_at - created_at`` inflates every
entry by the queue wait, and it inflates the slowest queues the most -- which
is backwards, because queue wait is a scheduling problem and toil is a labour
problem, and automating the work does not shorten a wait nobody was working
during.

So this collector derives handling time in a stated order of preference:

1.  **A worklog or time-spent field**, when the export carries one. Somebody
    recorded the minutes; the basis is ``measured``.
2.  **First response to resolution**, which brackets the interval the ticket
    was actually somebody's problem. This is still an *upper* bound -- a
    ticket can be assigned and untouched for a day -- so the basis is
    ``estimated`` and the direction of the error is recorded in the notes.
3.  **Nothing.** A ticket with neither contributes to the occurrence count,
    because it happened, and contributes no timing. It is not costed from its
    lifetime unless ``--allow-lifetime`` is passed, and passing it caps the
    cost basis at ``estimated`` and says in the emitted source field that the
    figure includes queue wait.

Tickets resolved after the window closes are counted but not timed: their
handling time is not yet known, and a partial duration would pull the median
down in exactly the cases where the work was slowest.

Field names are configurable because every tracker names these differently;
the defaults cover the common exports. The collector reads a file and holds no
credentials.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
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
    coerce_minutes,
    measure,
    pick,
    read_records,
    run,
    window_from_args,
)

#: Candidate field names, in the order they are tried. A tracker that uses a
#: name not listed here is handled with the ``--field-*`` overrides rather than
#: by editing this module.
REF_FIELDS: Final[tuple[str, ...]] = ("key", "id", "ticket", "number", "reference")
OPENED_FIELDS: Final[tuple[str, ...]] = ("created_at", "created", "opened_at", "fields.created")
RESOLVED_FIELDS: Final[tuple[str, ...]] = (
    "resolved_at",
    "resolutiondate",
    "closed_at",
    "fields.resolutiondate",
)
FIRST_RESPONSE_FIELDS: Final[tuple[str, ...]] = (
    "first_response_at",
    "first_responded_at",
    "assigned_at",
    "first_touch_at",
)
WORKLOG_FIELDS: Final[tuple[str, ...]] = (
    "time_spent_seconds",
    "timespent",
    "worklog_seconds",
    "fields.timespent",
)
QUEUE_FIELDS: Final[tuple[str, ...]] = ("queue", "project", "project_key", "fields.project.key")
TYPE_FIELDS: Final[tuple[str, ...]] = (
    "type",
    "issue_type",
    "request_type",
    "fields.issuetype.name",
)
LABEL_FIELDS: Final[tuple[str, ...]] = ("labels", "tags", "fields.labels")
WORKER_FIELDS: Final[tuple[str, ...]] = (
    "worklog_authors",
    "participants",
    "assignees",
    "assignee",
    "fields.assignee.name",
)


@dataclass(frozen=True)
class FieldMap:
    """Which export fields this collector reads, after any overrides."""

    ref: tuple[str, ...] = REF_FIELDS
    opened: tuple[str, ...] = OPENED_FIELDS
    resolved: tuple[str, ...] = RESOLVED_FIELDS
    first_response: tuple[str, ...] = FIRST_RESPONSE_FIELDS
    worklog: tuple[str, ...] = WORKLOG_FIELDS
    queue: tuple[str, ...] = QUEUE_FIELDS
    kind: tuple[str, ...] = TYPE_FIELDS
    labels: tuple[str, ...] = LABEL_FIELDS
    workers: tuple[str, ...] = WORKER_FIELDS


@dataclass(frozen=True)
class TicketFilter:
    """The saved-query equivalent, expressed so the collector can restate it.

    The filter is echoed into the emitted ``source`` field. A measurement whose
    source says only "the ticket export" cannot be reproduced; one that says
    which queue, type and labels were selected can be.
    """

    queue: str | None = None
    kind: str | None = None
    require_labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    require_resolved: bool = False

    def describe(self) -> str:
        parts: list[str] = []
        if self.queue:
            parts.append(f"queue={self.queue}")
        if self.kind:
            parts.append(f"type={self.kind}")
        if self.require_labels:
            parts.append("labels=" + "+".join(self.require_labels))
        if self.exclude_labels:
            parts.append("not-labels=" + "+".join(self.exclude_labels))
        if self.require_resolved:
            parts.append("resolved-only")
        return ", ".join(parts) if parts else "no filter"

    def matches(self, record: dict[str, Any], fields: FieldMap) -> bool:
        if self.queue is not None and _text(pick(record, fields.queue)) != self.queue:
            return False
        if self.kind is not None and _text(pick(record, fields.kind)) != self.kind:
            return False
        labels = _labels(pick(record, fields.labels))
        if any(label not in labels for label in self.require_labels):
            return False
        return not any(label in labels for label in self.exclude_labels)


@dataclass(frozen=True)
class Handling:
    """A derived handling time and the route it was derived by."""

    minutes: float | None
    basis: Basis
    route: str


def _text(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        for key in ("name", "key", "value", "displayName"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def _labels(value: object) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list):
        return frozenset(text for text in (_text(item) for item in value) if text is not None)
    return frozenset()


def count_workers(value: object) -> int:
    """How many people touched an occurrence, from whatever the export carried.

    A list of worklog authors is counted by distinct name; a single assignee
    counts as one. An absent field counts as one rather than zero: somebody
    did the work even when the export does not say who, and a zero here would
    cost the occurrence at nothing.
    """
    if isinstance(value, list):
        names = {text for text in (_text(item) for item in value) if text is not None}
        return max(1, len(names))
    return 1


def derive_handling(
    record: dict[str, Any],
    fields: FieldMap,
    *,
    ref: str,
    opened_at: datetime,
    window_end_exclusive: datetime,
    allow_lifetime: bool,
    worklog_unit: str,
) -> Handling:
    """Derive handling time by the documented order of preference."""
    worklog = coerce_minutes(
        pick(record, fields.worklog), context=f"ticket {ref} worklog", unit=worklog_unit
    )
    if worklog is not None:
        return Handling(minutes=worklog, basis="measured", route="worklog")

    resolved_raw = pick(record, fields.resolved)
    if resolved_raw is None:
        return Handling(minutes=None, basis="estimated", route="unresolved")
    resolved_at = coerce_datetime(resolved_raw, context=f"ticket {ref} resolution")
    if resolved_at >= window_end_exclusive:
        return Handling(minutes=None, basis="estimated", route="resolved-after-window")
    if resolved_at < opened_at:
        raise CollectorError(
            f"ticket {ref}: resolved at {resolved_at.isoformat()} before it was opened at "
            f"{opened_at.isoformat()}; the export's timestamps disagree"
        )

    first_response_raw = pick(record, fields.first_response)
    if first_response_raw is not None:
        first_response_at = coerce_datetime(
            first_response_raw, context=f"ticket {ref} first response"
        )
        if opened_at <= first_response_at <= resolved_at:
            minutes = (resolved_at - first_response_at).total_seconds() / 60.0
            if minutes > 0:
                return Handling(minutes=minutes, basis="estimated", route="response-to-resolution")

    if allow_lifetime:
        minutes = (resolved_at - opened_at).total_seconds() / 60.0
        if minutes > 0:
            return Handling(minutes=minutes, basis="estimated", route="lifetime")
    return Handling(minutes=None, basis="estimated", route="no-timing")


def collect(args: argparse.Namespace) -> Measurement:
    """Read the export, reconstruct occurrences, and measure them."""
    window = window_from_args(args)
    business_hours = business_hours_from_args(args)
    fields = _field_map(args)
    ticket_filter = TicketFilter(
        queue=args.queue,
        kind=args.ticket_type,
        require_labels=tuple(args.require_labels),
        exclude_labels=tuple(args.exclude_labels),
        require_resolved=bool(args.resolved_only),
    )
    records = read_records(args.export, args.records_key)

    window_end_exclusive = window.end_exclusive(business_hours.utc_offset_minutes)

    observations: list[Observation] = []
    routes: dict[str, int] = {}
    seen: set[str] = set()
    filtered_out = 0
    duplicates = 0
    unresolved = 0

    for index, record in enumerate(records):
        if not ticket_filter.matches(record, fields):
            filtered_out += 1
            continue
        resolved_present = pick(record, fields.resolved) is not None
        if ticket_filter.require_resolved and not resolved_present:
            filtered_out += 1
            unresolved += 1
            continue
        ref = _text(pick(record, fields.ref)) or f"record-{index}"
        if ref in seen:
            duplicates += 1
            continue
        seen.add(ref)
        opened_at = coerce_datetime(
            pick(record, fields.opened), context=f"ticket {ref} opened timestamp"
        )
        handling = derive_handling(
            record,
            fields,
            ref=ref,
            opened_at=opened_at,
            window_end_exclusive=window_end_exclusive,
            allow_lifetime=bool(args.allow_lifetime),
            worklog_unit=str(args.worklog_unit),
        )
        if handling.route == "unresolved":
            unresolved += 1
        routes[handling.route] = routes.get(handling.route, 0) + 1
        observations.append(
            Observation(
                ref=ref,
                started_at=opened_at,
                handling_minutes=handling.minutes,
                handling_basis=handling.basis,
                responders=count_workers(pick(record, fields.workers)),
            )
        )

    notes = _route_notes(routes, duplicates, filtered_out, unresolved, len(records))
    source = f"{args.source} [{ticket_filter.describe()}]"
    return measure(
        item_id=args.item_id,
        observations=observations,
        window=window,
        source=source,
        business_hours=business_hours,
        complete_enumeration=not args.sampled,
        extra_notes=notes,
    )


def _route_notes(
    routes: dict[str, int],
    duplicates: int,
    filtered_out: int,
    unresolved: int,
    total: int,
) -> list[Note]:
    """Say which route produced the timings, because the routes differ in kind."""
    notes: list[Note] = [
        Note(
            LEVEL_NOTE,
            f"read {total} records; {filtered_out} excluded by the filter, "
            f"{duplicates} duplicate references collapsed",
        )
    ]
    if routes:
        breakdown = ", ".join(f"{route}={count}" for route, count in sorted(routes.items()))
        notes.append(Note(LEVEL_NOTE, f"handling-time routes: {breakdown}"))
    if routes.get("response-to-resolution"):
        notes.append(
            Note(
                LEVEL_WARNING,
                f"{routes['response-to-resolution']} occurrence(s) timed from first response to "
                "resolution rather than from a worklog; that interval is an upper bound on "
                "engaged time, so the spread errs high and the cost basis is 'estimated'",
            )
        )
    if routes.get("lifetime"):
        notes.append(
            Note(
                LEVEL_WARNING,
                f"{routes['lifetime']} occurrence(s) timed from the full ticket lifetime, which "
                "includes queue wait nobody was working during; this overstates engaged time and "
                "should be replaced by worklog data before the entry claims a measured cost",
            )
        )
    if routes.get("resolved-after-window"):
        notes.append(
            Note(
                LEVEL_NOTE,
                f"{routes['resolved-after-window']} occurrence(s) resolved after the window "
                "closed; "
                "counted but not timed, since a truncated duration would pull the median down "
                "precisely where the work was slowest",
            )
        )
    if unresolved:
        notes.append(
            Note(
                LEVEL_NOTE,
                f"{unresolved} ticket(s) were still open; they contribute to the occurrence count "
                "because the work arrived, and contribute no timing",
            )
        )
    return notes


def _field_map(args: argparse.Namespace) -> FieldMap:
    """Apply any ``--field-*`` overrides over the defaults."""
    overrides: dict[str, tuple[str, ...]] = {}
    for name in ("ref", "opened", "resolved", "first_response", "worklog", "workers"):
        value = getattr(args, f"field_{name}", None)
        if value:
            overrides[name] = (str(value),)
    return FieldMap(**overrides)


def build() -> tuple[argparse.ArgumentParser, Callable[[argparse.Namespace], Measurement]]:
    parser = argparse.ArgumentParser(
        description="Measure toil frequency and handling cost from a ticket export.",
        epilog="Handling time is taken from a worklog where one exists, from first response to "
        "resolution otherwise, and from nothing at all rather than from a ticket's lifetime.",
    )
    add_common_arguments(parser)
    parser.add_argument("--queue", help="only tickets in this queue or project")
    parser.add_argument("--ticket-type", help="only tickets of this type")
    parser.add_argument(
        "--require-label",
        action="append",
        default=[],
        dest="require_labels",
        help="only tickets carrying this label; repeatable, all must be present",
    )
    parser.add_argument(
        "--exclude-label",
        action="append",
        default=[],
        dest="exclude_labels",
        help="drop tickets carrying this label; repeatable",
    )
    parser.add_argument(
        "--resolved-only",
        action="store_true",
        help="exclude tickets still open, which lowers the count as well as the timings",
    )
    parser.add_argument(
        "--allow-lifetime",
        action="store_true",
        help="cost tickets with no worklog and no first-response timestamp from their full "
        "lifetime; includes queue wait and overstates engaged time",
    )
    parser.add_argument(
        "--worklog-unit",
        choices=("seconds", "minutes", "hours"),
        default="seconds",
        help="unit of the worklog field (default: %(default)s)",
    )
    for name in ("ref", "opened", "resolved", "first-response", "worklog", "workers"):
        parser.add_argument(
            f"--field-{name}",
            dest=f"field_{name.replace('-', '_')}",
            metavar="FIELD",
            help=f"export field holding the {name.replace('-', ' ')} value",
        )
    return parser, collect


def main(argv: Sequence[str] | None = None) -> int:
    return run(build, argv)


if __name__ == "__main__":
    raise SystemExit(main())
