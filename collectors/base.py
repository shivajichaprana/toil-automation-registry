"""Shared machinery for turning operational exports into registry measurements.

A registry entry that claims ``basis: measured`` is making a checkable
statement: somebody counted occurrences over a stated window and named the
source. Typing that by hand is how an estimate ends up wearing a measurement's
label, so the collectors in this package derive the ``frequency`` and ``cost``
blocks from an export and fill the window, the source and the basis from what
they actually saw.

What the collectors deliberately do **not** produce is an annual total. The
registry stores inputs and derives totals on read (see the repository README),
and a collector that emitted a total would reintroduce the stored derivation
that the schema exists to prevent. The output of a collector is therefore a
*fragment* -- the two input blocks -- and nothing downstream of them.

Three decisions in here are policy rather than arithmetic, and they are stated
once so a reviewer can disagree with them in one place:

**The period is chosen, not fixed.** A rate of three occurrences in ninety days
written as ``0.03 per day`` is unreadable and unreviewable. The emitted period
is the smallest one that yields a count of at least one, and a period longer
than the observation window is marked as the extrapolation it is.

**A percentile needs a sample.** The ninetieth percentile of four timings is
the largest of four timings wearing a percentile's name. Below a stated sample
size the spread is reported as median-to-maximum and the basis is downgraded,
because the alternative is a number that looks measured and is not.

**A timestamp without a zone is refused.** The out-of-hours share decides a
weighting on every derived figure, and a naive timestamp read as UTC moves
every occurrence by the exporter's offset. Observations carry aware datetimes
or they do not enter the measurement.

Collectors read exported files. They hold no credentials and open no network
connections, so a measurement can be reproduced from the export that produced
it and a repository checkout is not a thing that needs access to production.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal, cast

LOGGER: Final = logging.getLogger("toil.collect")

Basis = Literal["measured", "sampled", "estimated"]

#: Weakest-first ordering. A measurement assembled from several inputs is only
#: as strong as its weakest input, so ``weakest`` resolves the emitted basis
#: rather than letting the most flattering contributor name it.
BASIS_ORDER: Final[tuple[Basis, ...]] = ("estimated", "sampled", "measured")

#: Length in days of each period the schema accepts. These are the exact
#: reciprocals of the annualiser in ``tools/validate_registry.py``
#: (``PER_YEAR``), which is what makes the emitted count round-trip: the
#: annualised occurrence figure the validator derives is independent of which
#: period a collector chose to express the rate in. ``check_period_constants``
#: asserts the relationship instead of trusting this comment.
DAYS_PER_YEAR: Final[float] = 365.0
PERIOD_DAYS: Final[dict[str, float]] = {
    "day": DAYS_PER_YEAR / 365.0,
    "week": DAYS_PER_YEAR / 52.0,
    "month": DAYS_PER_YEAR / 12.0,
    "quarter": DAYS_PER_YEAR / 4.0,
    "year": DAYS_PER_YEAR / 1.0,
}

#: Periods in ascending length. The emitted period is the first of these that
#: produces a count of at least ``MIN_READABLE_COUNT``.
PERIODS_ASCENDING: Final[tuple[str, ...]] = ("day", "week", "month", "quarter", "year")

#: A count below this reads as a decimal nobody can check against a source.
MIN_READABLE_COUNT: Final[float] = 1.0

#: Timings needed before a ninetieth percentile is reported as one. Below this
#: the spread is reported as median-to-maximum and the basis is downgraded.
MIN_SAMPLES_FOR_PERCENTILE: Final[int] = 10

#: Timings needed before a separate ``max`` is worth recording alongside the
#: spread. With fewer, the maximum *is* the upper end of the spread and
#: repeating it would imply two observations where there is one.
MIN_SAMPLES_FOR_MAX: Final[int] = 5

#: Difference between the observed mean responder count and the integer the
#: schema accepts, above which the loss is called out in the emitted notes.
RESPONDER_ROUNDING_TOLERANCE: Final[float] = 0.1

LEVEL_WARNING: Final = "warning"
LEVEL_NOTE: Final = "note"


class CollectorError(Exception):
    """An input problem that prevents a measurement from being produced."""


def weakest(bases: Iterable[Basis]) -> Basis:
    """Return the weakest basis in ``bases``, defaulting to ``estimated``."""
    present = list(bases)
    if not present:
        return "estimated"
    return min(present, key=BASIS_ORDER.index)


def check_period_constants() -> list[str]:
    """Verify the period lengths against the validator's annualiser.

    The collector divides by a period length to produce a count; the validator
    multiplies by ``PER_YEAR`` to annualise it. If the two ever disagree, a
    collected count silently annualises to something other than the occurrences
    that were counted -- and the only visible symptom is a number that is
    slightly wrong. Returns a list of mismatches, empty when consistent.
    """
    expected: Final[dict[str, float]] = {
        "day": 365.0,
        "week": 52.0,
        "month": 12.0,
        "quarter": 4.0,
        "year": 1.0,
    }
    problems: list[str] = []
    if set(expected) != set(PERIOD_DAYS):
        problems.append(
            f"period sets differ: collector has {sorted(PERIOD_DAYS)}, "
            f"validator has {sorted(expected)}"
        )
        return problems
    for period, per_year in expected.items():
        product = PERIOD_DAYS[period] * per_year
        if not math.isclose(product, DAYS_PER_YEAR, rel_tol=1e-12):
            problems.append(
                f"{period}: {PERIOD_DAYS[period]} days x {per_year} per year = {product}, "
                f"expected {DAYS_PER_YEAR}"
            )
    return problems


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BusinessHours:
    """When work is cheap, expressed as a calendar rather than an hour range.

    The out-of-hours share weights every derived figure for the entry, so the
    classification has to survive the obvious objections: a Saturday afternoon
    is out of hours, a public holiday is out of hours, and the hour that counts
    is the hour where the responder is, not the hour the exporter wrote.
    """

    utc_offset_minutes: int = 0
    start_hour: int = 9
    end_hour: int = 18
    workdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        if not 0 <= self.start_hour < self.end_hour <= 24:  # noqa: PLR2004 - clock bounds
            raise CollectorError(
                f"business hours {self.start_hour}-{self.end_hour} are not a forward "
                "interval inside a 24-hour day"
            )
        if not self.workdays:
            raise CollectorError("no workdays declared: every occurrence would be out of hours")

    def local(self, instant: datetime) -> datetime:
        """Convert an aware instant into the responder's local wall clock."""
        if instant.tzinfo is None:
            raise CollectorError(
                f"{instant.isoformat()} has no timezone; a naive timestamp read as UTC "
                "shifts every occurrence by the exporter's offset and silently rewrites "
                "the out-of-hours share"
            )
        return instant.astimezone(timezone(timedelta(minutes=self.utc_offset_minutes)))

    def is_out_of_hours(self, instant: datetime) -> bool:
        """True when ``instant`` falls outside the declared working calendar."""
        moment = self.local(instant)
        if moment.date() in self.holidays:
            return True
        if moment.weekday() not in self.workdays:
            return True
        return not self.start_hour <= moment.hour < self.end_hour


@dataclass(frozen=True)
class ObservationWindow:
    """The period a count was taken over, inclusive of both end dates.

    Inclusive because the exports are filtered on dates, not instants: a query
    for ``2026-09-01`` to ``2026-09-30`` returns thirty days of occurrences,
    and calling that twenty-nine would understate every rate derived from it.
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise CollectorError(f"window {self.start}..{self.end} runs backwards")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, day: date) -> bool:
        """True when ``day`` -- a local calendar date -- falls in the window.

        A date rather than an instant on purpose. The window is a calendar
        window in the responder's timezone, so the caller localises the instant
        first; comparing an instant's own ``.date()`` would place a late-evening
        occurrence in yesterday or tomorrow depending on the offset the
        exporter happened to write.
        """
        return self.start <= day <= self.end

    def end_exclusive(self, utc_offset_minutes: int) -> datetime:
        """First instant after the window, on the responder's local clock."""
        return datetime.combine(
            self.end + timedelta(days=1),
            time.min,
            tzinfo=timezone(timedelta(minutes=utc_offset_minutes)),
        )

    def describe(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()} ({self.days} days)"


@dataclass(frozen=True)
class Observation:
    """One occurrence of the work, as a collector managed to reconstruct it.

    ``handling_minutes`` is ``None`` when the export carried nothing that could
    stand in for engaged time. Such an occurrence still counts towards the
    frequency -- it happened -- but contributes no timing, and a measurement
    assembled mostly from these says so in its notes.
    """

    ref: str
    started_at: datetime
    handling_minutes: float | None = None
    handling_basis: Basis = "estimated"
    responders: int = 1

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None:
            raise CollectorError(f"observation {self.ref}: started_at carries no timezone")
        if self.handling_minutes is not None and self.handling_minutes <= 0:
            raise CollectorError(
                f"observation {self.ref}: handling time {self.handling_minutes} is not positive; "
                "an occurrence that consumed no time is not an occurrence"
            )
        if self.responders < 1:
            raise CollectorError(f"observation {self.ref}: responders {self.responders} is below 1")


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Note:
    """Something a reviewer of the emitted fragment needs to know."""

    level: str
    message: str

    def render(self) -> str:
        return f"{self.level.upper():<7} {self.message}"


@dataclass(frozen=True)
class Spread:
    """A handling-time spread and an honest account of where it came from."""

    p50: float
    p90: float
    max_observed: float | None
    sample_size: int
    basis: Basis
    interpolated: bool


def quantile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between the closest order statistics.

    Stated rather than inherited: with ``n`` sorted samples the quantile sits
    at position ``(n - 1) * q``, and a fractional position interpolates between
    its neighbours. This is reproducible by hand from the sample in the
    evidence block, which a method that fitted a distribution would not be.
    """
    if not values:
        raise CollectorError("cannot take a quantile of an empty sample")
    if not 0.0 <= q <= 1.0:
        raise CollectorError(f"quantile {q} is outside [0, 1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def spread_from(timings: Sequence[float], contributing_basis: Basis) -> Spread | None:
    """Derive a handling-time spread, downgrading what the sample cannot support.

    The ninetieth percentile of a small sample is dominated by its largest
    element, so below ``MIN_SAMPLES_FOR_PERCENTILE`` the upper end is reported
    as the observed maximum and the basis is pulled down to ``sampled`` at
    best. The registry can hold an honest median-to-worst bracket; what it must
    not hold is a percentile computed from four numbers.
    """
    if not timings:
        return None
    ordered = sorted(timings)
    median = quantile(ordered, 0.5)
    observed_max = ordered[-1]
    enough = len(ordered) >= MIN_SAMPLES_FOR_PERCENTILE
    upper = quantile(ordered, 0.9) if enough else observed_max
    basis: Basis = contributing_basis if enough else weakest((contributing_basis, "sampled"))
    keep_max = len(ordered) >= MIN_SAMPLES_FOR_MAX and observed_max > upper
    return Spread(
        p50=round(median, 1),
        p90=round(upper, 1),
        max_observed=round(observed_max, 1) if keep_max else None,
        sample_size=len(ordered),
        basis=basis,
        interpolated=enough,
    )


@dataclass(frozen=True)
class Period:
    """The period a rate is expressed in, and whether saying so overreached."""

    name: str
    count: float
    extrapolated: bool


def choose_period(occurrences: int, window_days: int) -> Period:
    """Express a rate in the smallest period that yields a readable count.

    Two failure modes are being avoided. A period that is too short produces a
    count like ``0.033 per day``, which no reviewer can check against a ticket
    query. A period that is longer than the observation window produces a count
    the window cannot support -- four per quarter from a fortnight's data is an
    extrapolation, and it is returned marked as one rather than silently.
    """
    if occurrences <= 0:
        raise CollectorError("no occurrences to express as a rate")
    if window_days <= 0:
        raise CollectorError("observation window is empty")
    rate_per_day = occurrences / window_days
    for name in PERIODS_ASCENDING:
        length = PERIOD_DAYS[name]
        count = rate_per_day * length
        if count >= MIN_READABLE_COUNT:
            return Period(name=name, count=count, extrapolated=length > window_days)
    name = PERIODS_ASCENDING[-1]
    length = PERIOD_DAYS[name]
    return Period(name=name, count=rate_per_day * length, extrapolated=length > window_days)


@dataclass
class Measurement:
    """A ``frequency`` and ``cost`` fragment, with its provenance attached."""

    item_id: str
    window: ObservationWindow
    source: str
    observations: int
    period: Period
    frequency_basis: Basis
    spread: Spread | None
    responders: int
    responder_mean: float
    out_of_hours_fraction: float
    timing_sample: int
    notes: list[Note] = field(default_factory=list)

    @property
    def cost_basis(self) -> Basis:
        return self.spread.basis if self.spread else "estimated"

    def frequency_fragment(self) -> dict[str, Any]:
        """The ``frequency`` block, shaped exactly as the schema requires."""
        fragment: dict[str, Any] = {
            "count": round(self.period.count, 2),
            "per": self.period.name,
            "basis": self.frequency_basis,
            "window_days": self.window.days,
            "source": self.source,
        }
        derivation = (
            f"{self.observations} occurrences over {self.window.describe()}, "
            f"expressed per {self.period.name}"
        )
        if self.period.extrapolated:
            derivation += (
                "; the period is longer than the window, so the count is extrapolated "
                "and the basis is downgraded accordingly"
            )
        fragment["notes"] = derivation
        return fragment

    def cost_fragment(self) -> dict[str, Any] | None:
        """The ``cost`` block, or ``None`` when nothing timed an occurrence."""
        if self.spread is None:
            return None
        handling: dict[str, float] = {"p50": self.spread.p50, "p90": self.spread.p90}
        if self.spread.max_observed is not None:
            handling["max"] = self.spread.max_observed
        upper_kind = (
            "interpolated ninetieth percentile" if self.spread.interpolated else "observed maximum"
        )
        provenance = f"{self.spread.sample_size} timed occurrences; upper end is the {upper_kind}"
        return {
            "handling_minutes": handling,
            "responders": self.responders,
            "out_of_hours_fraction": round(self.out_of_hours_fraction, 2),
            "basis": self.spread.basis,
            "source": f"{self.source} -- {provenance}",
        }

    def fragment(self) -> dict[str, Any]:
        """Both blocks together, ready to merge into a registry entry."""
        fragment: dict[str, Any] = {"frequency": self.frequency_fragment()}
        cost = self.cost_fragment()
        if cost is not None:
            fragment["cost"] = cost
        return fragment

    def render(self) -> str:
        """Human-readable account of what was measured and what it rests on."""
        lines = [
            f"item            {self.item_id}",
            f"window          {self.window.describe()}",
            f"occurrences     {self.observations}",
            f"rate            {self.period.count:.2f} per {self.period.name}"
            + ("  (extrapolated beyond the window)" if self.period.extrapolated else ""),
            f"frequency basis {self.frequency_basis}",
        ]
        if self.spread is None:
            lines.append("handling time   not derivable from this export")
        else:
            upper = "p90" if self.spread.interpolated else "max of sample"
            lines.append(
                f"handling time   p50 {self.spread.p50} min, upper {self.spread.p90} min "
                f"({upper}, n={self.spread.sample_size})"
            )
            lines.append(f"cost basis      {self.spread.basis}")
        lines.append(
            f"responders      {self.responders}  (observed mean {self.responder_mean:.2f})"
        )
        lines.append(f"out of hours    {self.out_of_hours_fraction:.0%} of occurrences")
        if self.notes:
            lines.append("")
            lines.extend(note.render() for note in self.notes)
        return "\n".join(lines)


def measure(
    *,
    item_id: str,
    observations: Sequence[Observation],
    window: ObservationWindow,
    source: str,
    business_hours: BusinessHours,
    complete_enumeration: bool = True,
    extra_notes: Sequence[Note] = (),
) -> Measurement:
    """Assemble a measurement from reconstructed occurrences.

    ``complete_enumeration`` records whether the export is believed to contain
    every occurrence in the window. A collector that filtered a sample, or that
    read an export the source had already truncated, passes ``False`` and the
    frequency basis becomes ``sampled``: a count that missed occurrences is not
    a measured rate, and the distinction is the difference between a figure a
    reviewer can defend and one they cannot.
    """
    problems = check_period_constants()
    if problems:
        raise CollectorError(
            "period constants disagree with the validator's annualiser, so a collected "
            "count would not annualise to the occurrences counted: " + "; ".join(problems)
        )

    inside = [
        obs for obs in observations if window.contains(business_hours.local(obs.started_at).date())
    ]
    notes: list[Note] = list(extra_notes)
    dropped = len(observations) - len(inside)
    if dropped:
        notes.append(
            Note(
                LEVEL_WARNING,
                f"{dropped} occurrence(s) fell outside {window.describe()} and were excluded; "
                "the export and the declared window disagree about what was queried",
            )
        )
    if not inside:
        raise CollectorError(
            f"no occurrences inside {window.describe()}: the schema requires a positive "
            "count, so an item with no occurrences in the window is either not toil or "
            "needs a longer window -- it is not an entry with a rate of zero"
        )

    period = choose_period(len(inside), window.days)
    frequency_basis: Basis = "measured" if complete_enumeration else "sampled"
    if period.extrapolated:
        frequency_basis = weakest((frequency_basis, "sampled"))
        notes.append(
            Note(
                LEVEL_WARNING,
                f"rate expressed per {period.name} from a {window.days}-day window; the "
                "period exceeds the window, so the count is an extrapolation and the "
                "frequency basis is capped at 'sampled'",
            )
        )

    timings = [obs.handling_minutes for obs in inside if obs.handling_minutes is not None]
    timing_basis = weakest(
        [obs.handling_basis for obs in inside if obs.handling_minutes is not None]
    )
    spread = spread_from(timings, timing_basis)
    if spread is None:
        notes.append(
            Note(
                LEVEL_WARNING,
                "no occurrence in the export carried anything that could stand in for "
                "engaged time, so no cost block was produced; the frequency is usable on "
                "its own and the handling spread has to be timed by hand",
            )
        )
    elif not spread.interpolated:
        notes.append(
            Note(
                LEVEL_WARNING,
                f"{spread.sample_size} timed occurrences is below the {MIN_SAMPLES_FOR_PERCENTILE} "
                "needed for a ninetieth percentile to mean anything, so the upper end of the "
                "spread is the observed maximum and the cost basis is capped at 'sampled'",
            )
        )
    untimed = len(inside) - len(timings)
    if untimed and spread is not None:
        notes.append(
            Note(
                LEVEL_NOTE,
                f"{untimed} of {len(inside)} occurrences carried no timing and contributed to "
                "the count but not to the spread",
            )
        )

    responder_mean = sum(obs.responders for obs in inside) / len(inside)
    responders = max(1, int(math.floor(responder_mean + 0.5)))
    if abs(responder_mean - responders) > RESPONDER_ROUNDING_TOLERANCE:
        notes.append(
            Note(
                LEVEL_NOTE,
                f"observed mean of {responder_mean:.2f} responders per occurrence was rounded to "
                f"the integer {responders} the schema accepts; the difference is real load that "
                "the field cannot express",
            )
        )

    out_of_hours = sum(1 for obs in inside if business_hours.is_out_of_hours(obs.started_at))
    LOGGER.info(
        "%s: %d occurrences over %d days, %d timed, %d out of hours",
        item_id,
        len(inside),
        window.days,
        len(timings),
        out_of_hours,
    )
    return Measurement(
        item_id=item_id,
        window=window,
        source=source,
        observations=len(inside),
        period=period,
        frequency_basis=frequency_basis,
        spread=spread,
        responders=responders,
        responder_mean=responder_mean,
        out_of_hours_fraction=out_of_hours / len(inside),
        timing_sample=len(timings),
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Reading exports
# --------------------------------------------------------------------------- #


def read_records(path: Path, records_key: str | None = None) -> list[dict[str, Any]]:
    """Read an export as newline-delimited JSON, a JSON array, or one object.

    Both of the first two shapes are accepted because both are what export
    tools produce, and converting between them before a measurement can be
    taken is work nobody should have to do.

    A top-level **object** is read as a single record, and an API response
    envelope -- one object wrapping the records under a key -- is unwrapped
    only when ``records_key`` names that key. The alternative is to detect an
    envelope by looking for an array inside the object, and that cannot be done
    safely: a single exported ticket carrying a ``labels`` list is one object
    with one array in it and is indistinguishable from an envelope by shape.
    Guessing there would reject a valid export, or worse, measure a list of
    label strings. So the wrapper is named rather than inferred, and a likely
    envelope read as a record is called out in the log instead.
    """
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise CollectorError(f"{path} is empty")
    payload = _parse_whole(text)
    if isinstance(payload, list):
        return _as_record_list(payload, path)
    if isinstance(payload, dict):
        record = cast(dict[str, Any], payload)
        if records_key is not None:
            return _unwrap(record, records_key, path)
        _warn_if_envelope(record, path)
        return [record]
    if payload is not None:
        raise CollectorError(
            f"{path}: top level is {type(payload).__name__}, expected an object or an array"
        )
    return _read_ndjson(text, path)


def _parse_whole(text: str) -> object | None:
    """Parse the whole file as one JSON value, or return ``None`` if it is not."""
    try:
        return cast(object, json.loads(text))
    except json.JSONDecodeError:
        return None


def _read_ndjson(text: str, path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise CollectorError(f"{path}:{number}: not valid JSON: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise CollectorError(
                f"{path}:{number}: expected an object, found {type(record).__name__}"
            )
        records.append(cast(dict[str, Any], record))
    if not records:
        raise CollectorError(f"{path} contained no records")
    return records


def _as_record_list(payload: object, path: Path) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise CollectorError(f"{path}: expected an array of objects")
    records: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise CollectorError(f"{path}[{index}]: expected an object")
        records.append(cast(dict[str, Any], item))
    if not records:
        raise CollectorError(f"{path} contained no records")
    return records


def _unwrap(record: dict[str, Any], records_key: str, path: Path) -> list[dict[str, Any]]:
    """Pull the records out of a named envelope key, which may be dotted."""
    inner = pick(record, [records_key])
    if inner is None:
        raise CollectorError(
            f"{path}: no key {records_key!r} in the top-level object; its keys are {sorted(record)}"
        )
    if not isinstance(inner, list):
        raise CollectorError(
            f"{path}: {records_key!r} holds {type(inner).__name__}, not an array of records"
        )
    return _as_record_list(inner, path)


def _warn_if_envelope(record: dict[str, Any], path: Path) -> None:
    """Point at ``--records-key`` when a single record looks like a wrapper."""
    candidates = [
        key
        for key, value in record.items()
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value)
    ]
    if candidates:
        LOGGER.warning(
            "%s was read as a single record; if it is a response envelope, name the records "
            "with --records-key (candidates: %s)",
            path,
            ", ".join(sorted(candidates)),
        )


def pick(record: dict[str, Any], names: Sequence[str]) -> Any:  # noqa: ANN401 - export values
    """Return the first present, non-null value among ``names``.

    Export field names vary between tools and between versions of the same
    tool, so a collector names the candidates it understands rather than
    requiring the export to be reshaped first. Dotted names walk into nested
    objects, which is how most APIs nest a user or a status.
    """
    for name in names:
        value: Any = record
        for part in name.split("."):
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value is not None:
            return value
    return None


def coerce_datetime(value: object, *, context: str) -> datetime:
    """Parse an export timestamp into an aware datetime, or refuse it.

    A trailing ``Z`` is accepted because exporters emit it and
    ``fromisoformat`` did not accept it before Python 3.11. An epoch value is
    accepted as UTC, which is what epochs are. A timestamp with no offset at
    all is refused: guessing is what makes the out-of-hours share wrong.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise CollectorError(f"{context}: missing timestamp")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CollectorError(f"{context}: {value!r} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CollectorError(
            f"{context}: {value!r} carries no UTC offset; re-export with offsets rather "
            "than letting the collector assume one"
        )
    return parsed


def coerce_minutes(value: object, *, context: str, unit: str) -> float | None:
    """Convert a duration field to minutes, returning ``None`` when absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            value = float(stripped)
        except ValueError as exc:
            raise CollectorError(f"{context}: {value!r} is not a number") from exc
    if not isinstance(value, (int, float)):
        raise CollectorError(f"{context}: {value!r} is not a number")
    factors: Final[dict[str, float]] = {"seconds": 1.0 / 60.0, "minutes": 1.0, "hours": 60.0}
    if unit not in factors:
        raise CollectorError(f"{context}: unknown duration unit {unit!r}")
    minutes = float(value) * factors[unit]
    return minutes if minutes > 0 else None


# --------------------------------------------------------------------------- #
# Writing back
# --------------------------------------------------------------------------- #


def apply_fragment(entry_path: Path, item_id: str, fragment: dict[str, Any]) -> list[str]:
    """Replace the measured blocks of a registry entry, in place.

    Only the blocks the collector derived are touched. Key order is preserved
    so the diff shows the change rather than a reordering, and the id in the
    file has to match the id the measurement was taken for -- writing a
    measurement into the wrong entry is the one mistake here that produces a
    plausible-looking file nothing would flag.
    """
    if not entry_path.is_file():
        raise CollectorError(f"{entry_path} does not exist")
    data = json.loads(entry_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CollectorError(f"{entry_path}: top level must be an object")
    entry = cast(dict[str, Any], data)
    declared = entry.get("id")
    if declared != item_id:
        raise CollectorError(
            f"{entry_path} holds id {declared!r} but the measurement is for {item_id!r}; "
            "refusing to write a measurement into another item's entry"
        )
    changed: list[str] = []
    for block, value in fragment.items():
        if entry.get(block) != value:
            changed.append(block)
        entry[block] = value
    entry_path.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LOGGER.info("updated %s: %s", entry_path, ", ".join(changed) or "no change")
    return changed


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the arguments every collector shares."""
    parser.add_argument("--export", type=Path, required=True, help="exported records to read")
    parser.add_argument("--id", required=True, dest="item_id", help="registry id being measured")
    parser.add_argument(
        "--from",
        dest="window_from",
        required=True,
        help="first date of the observation window (YYYY-MM-DD, inclusive)",
    )
    parser.add_argument(
        "--to",
        dest="window_to",
        required=True,
        help="last date of the observation window (YYYY-MM-DD, inclusive)",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="how to reproduce this measurement: the saved query or rule name it came from",
    )
    parser.add_argument(
        "--sampled",
        action="store_true",
        help="the export is a subset rather than every occurrence in the window, which caps "
        "the frequency basis at 'sampled'",
    )
    parser.add_argument(
        "--utc-offset-minutes",
        type=int,
        default=0,
        help="responder's offset from UTC, used to place occurrences on a local clock "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--business-hours",
        default="9-18",
        help="local working hours as START-END on a 24-hour clock (default: %(default)s)",
    )
    parser.add_argument(
        "--workdays",
        default="0,1,2,3,4",
        help="working weekdays, Monday as 0 (default: %(default)s)",
    )
    parser.add_argument(
        "--holiday",
        action="append",
        default=[],
        dest="holidays",
        metavar="YYYY-MM-DD",
        help="a non-working date; repeatable",
    )
    parser.add_argument(
        "--records-key",
        metavar="KEY",
        help="the export is a response envelope and the records live under this key; dotted "
        "names walk into nested objects",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit the fragment")
    parser.add_argument(
        "--apply",
        type=Path,
        metavar="ENTRY",
        help="write the derived blocks into this registry entry instead of printing them",
    )
    parser.add_argument("--verbose", action="store_true", help="log what was read and derived")


def window_from_args(args: argparse.Namespace) -> ObservationWindow:
    return ObservationWindow(
        start=parse_date(args.window_from, context="--from"),
        end=parse_date(args.window_to, context="--to"),
    )


def business_hours_from_args(args: argparse.Namespace) -> BusinessHours:
    bounds = str(args.business_hours).split("-")
    if len(bounds) != 2:  # noqa: PLR2004 - a range has two ends
        raise CollectorError(f"--business-hours {args.business_hours!r} is not START-END")
    try:
        start_hour, end_hour = int(bounds[0]), int(bounds[1])
    except ValueError as exc:
        raise CollectorError(f"--business-hours {args.business_hours!r} is not numeric") from exc
    try:
        workdays = frozenset(int(part) for part in str(args.workdays).split(",") if part.strip())
    except ValueError as exc:
        raise CollectorError(
            f"--workdays {args.workdays!r} is not a list of weekday numbers"
        ) from exc
    if any(day < 0 or day > 6 for day in workdays):  # noqa: PLR2004 - weekday bounds
        raise CollectorError(f"--workdays {args.workdays!r} holds a value outside 0..6")
    return BusinessHours(
        utc_offset_minutes=int(args.utc_offset_minutes),
        start_hour=start_hour,
        end_hour=end_hour,
        workdays=workdays,
        holidays=frozenset(parse_date(value, context="--holiday") for value in args.holidays),
    )


def parse_date(value: str, *, context: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CollectorError(f"{context}: {value!r} is not a YYYY-MM-DD date") from exc


def emit(measurement: Measurement, args: argparse.Namespace) -> int:
    """Render, apply or print a measurement according to the parsed arguments."""
    fragment = measurement.fragment()
    if args.apply is not None:
        changed = apply_fragment(args.apply, measurement.item_id, fragment)
        print(measurement.render())
        print()
        print(f"{args.apply}: {', '.join(changed) if changed else 'already up to date'}")
        return 0
    if args.as_json:
        print(json.dumps(fragment, indent=2, ensure_ascii=False))
        return 0
    print(measurement.render())
    print()
    print(json.dumps(fragment, indent=2, ensure_ascii=False))
    return 0


def run(build: Any, argv: Sequence[str] | None = None) -> int:  # noqa: ANN401 - collector callable
    """Shared entry point: parse, collect, emit, and report failures as text.

    Emitting is inside the error handling, not after it. Writing a fragment
    into a registry entry is the step most likely to refuse -- a mismatched id,
    an unreadable entry -- and a refusal there has to arrive as the same exit
    code and the same one-line message as a refusal while reading the export,
    rather than as a traceback from a tool a pipeline is gating on.

    Exit codes match the validator's so that a pipeline can treat them
    uniformly: ``0`` produced a measurement, ``2`` could not run.
    """
    parser, collect = build()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        measurement = collect(args)
        return emit(measurement, args)
    except CollectorError as exc:
        print(f"could not measure: {exc}")
        return 2
    except OSError as exc:
        print(f"could not read the export: {exc}")
        return 2
    except json.JSONDecodeError as exc:
        print(f"export is not valid JSON: {exc}")
        return 2
