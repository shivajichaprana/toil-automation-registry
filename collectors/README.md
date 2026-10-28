# collectors/

Each collector reads an exported file and produces the `frequency` and `cost`
blocks of a registry entry, with the observation window, the reproducible
source and the basis derived from what the export actually contained.

The point is not convenience. An entry that claims `basis: measured` is making
a checkable statement, and the fastest way to put a false one in the inventory
is to type the numbers by hand from a dashboard somebody glanced at. A
collector cannot stop a reviewer from overriding its output, but it can make
the honest version the easy one.

| module       | source                | what it corrects for                                   |
| ------------ | --------------------- | ------------------------------------------------------ |
| `tickets.py` | ticket/queue export   | resolution time is not engaged time                    |
| `alerts.py`  | alert-firing export   | a firing is not an occurrence, and noise is not toil    |
| `base.py`    | —                     | period choice, percentiles, out-of-hours, writing back  |

## What they will not do

**They do not emit a total.** The registry stores inputs and derives annual
load on read; a collector that wrote a total would reintroduce the stored
derivation the schema exists to prevent. The output is the two input blocks and
nothing downstream of them.

**They do not hold credentials and do not open a network connection.** Point
them at an export. A measurement is then reproducible from the file that
produced it, and a checkout of this repository is not a thing that needs
production access.

**They do not guess at a timestamp with no UTC offset.** The out-of-hours share
weights every derived figure for the entry, and a naive timestamp read as UTC
moves every occurrence by the exporter's offset. Such a record is refused with
the field named, not silently assumed.

## Usage

```bash
# Frequency and cost from a ticket export, printed for review.
python3 -m collectors.tickets \
  --export exports/ops-queue.ndjson \
  --id stuck-queue-manual-drain \
  --from 2026-07-01 --to 2026-09-29 \
  --source 'saved query: OPS toil, queue drains' \
  --queue OPS --require-label toil \
  --utc-offset-minutes 330 --business-hours 9-18

# Same, written straight into the entry.
python3 -m collectors.alerts \
  --export exports/lag-firings.json \
  --id stuck-queue-manual-drain \
  --from 2026-07-01 --to 2026-09-29 \
  --source 'alert-rule: notification-dispatch-consumer-lag-sustained' \
  --rule notification-dispatch-consumer-lag-sustained \
  --utc-offset-minutes 330 \
  --apply registry/stuck-queue-manual-drain.json
```

`--apply` replaces only the blocks the collector derived, preserves key order
so the diff shows the change rather than a reordering, and refuses to write
into an entry whose `id` is not the one being measured — which is the one
mistake here that would otherwise produce a plausible-looking file nothing
downstream would flag.

Exports are read as newline-delimited JSON, as a JSON array, or as a single
object. An API response envelope is unwrapped only when `--records-key` names
the key the records live under: a single exported ticket carrying a `labels`
list is one object with one array in it, indistinguishable by shape from an
envelope, so the wrapper is named rather than inferred.

Exit codes match the validator's: `0` produced a measurement, `2` could not
run. Every refusal arrives as one line of text, including a refusal to write
the fragment, so a pipeline gating on these never has to read a traceback.

## The three judgements that are policy

Each of these is a decision about what a number means, not arithmetic. They are
stated here and in the modules, once, so that disagreeing with one means
changing it in a single place and watching every measurement move.

### Handling time is engaged time, not elapsed time

A ticket opened on Monday and closed on Thursday did not consume three days of
a person. Costing an entry from `resolved_at - created_at` inflates it by the
queue wait, and it inflates the slowest queues most — which is backwards, since
queue wait is a scheduling problem and toil is a labour problem. Automating the
work does not shorten a wait nobody was working during.

So handling time comes from a worklog where one exists (`measured`); from first
response to resolution otherwise, which still brackets the interval high and is
therefore `estimated`; and from nothing at all rather than from a ticket's
lifetime. `--allow-lifetime` opts into the weakest route, and says in the
emitted source field what it included.

A ticket resolved after the window closes is counted and not timed. Its
handling time is not yet known, and a duration truncated at the window edge
would pull the median down in precisely the cases where the work was slowest.

### A firing is not an occurrence

An alert on a threshold being crossed repeatedly fires, resolves and fires
again; the responder handled one interruption. Counting rows makes the noisiest
rules rank highest as automation candidates on the strength of their noise.
Firings on the same rule and target arriving within a correlation window of the
previous one are folded into one occurrence, and the raw count, the folded count
and the difference between them are all reported.

A firing that cleared itself in ninety seconds without an acknowledgement cost
no engineer time. Counting it raises the frequency while lowering the median
handling time, so the entry's rank moves twice for a reason that is not load.
Such firings are dropped, and the proportion dropped is reported: a rule whose
firings mostly self-clear has an alert-quality problem, and the inventory
should not absorb it as toil.

Handling time inside an occurrence is the **union** of its engaged intervals,
not their sum. A flapping alert acknowledged once produces overlapping
intervals across its firings, and adding those up charges the same minute of
somebody's attention more than once.

### A number is only as strong as the sample behind it

The ninetieth percentile of four timings is the largest of four timings wearing
a percentile's name. Below a stated sample size the upper end of the spread is
reported as the observed maximum and the basis is capped at `sampled`. The
registry can hold an honest median-to-worst bracket; what it must not hold is a
percentile computed from four numbers.

The same rule governs the rate. A count expressed per quarter from a fortnight
of data is an extrapolation, so the emitted period is the smallest one that
yields a count of at least one, and a period longer than the window is marked
and the frequency basis capped. An item with no occurrences in the window does
not become an entry with a rate of zero — the collector refuses, because such
an item is either not toil or needs a longer window.

## Period lengths are tied to the validator

The collector divides by a period length to produce a count; the validator
multiplies by `PER_YEAR` to annualise it. The two sets of constants are exact
reciprocals, which is what makes the annualised occurrence figure independent
of the period a collector happened to choose. `check_period_constants()`
asserts the relationship and `measure()` refuses to run if it does not hold,
because the only visible symptom of a drift there would be a number that is
slightly wrong.
