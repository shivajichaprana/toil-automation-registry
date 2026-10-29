# toil-automation-registry

Toil inventory as code: measurement, ranked automation candidates, runbook
conversion, reduction tracking.

Most toil inventories are a spreadsheet of guesses that nobody revisits. This
repository treats the inventory as a reviewed artefact instead: every item is a
file, every file is validated against a schema in CI, and every number in it
carries a basis — measured, sampled or estimated — so a reader can tell the
difference between a figure somebody counted and a figure somebody felt.

## Why the inventory stores inputs, not totals

The usual field in a toil tracker is a single pre-multiplied total: *"roughly
six hours a month."* That number is the one thing you must not store, because
it is lossy in the exact dimension that matters.

Consider two items that both consume ten engineer-hours a year:

| item                       | occurrences / year | handling time each |
| -------------------------- | -----------------: | -----------------: |
| certificate rotation       |                  4 |          150 min   |
| stuck-queue drain          |                120 |            5 min   |

As totals they are identical, so a tracker that stores totals ranks them
equally. They are not equally expensive and they are not equally worth
automating:

- The second item interrupts someone **thirty times more often**. An interrupt
  costs more than the minutes it consumes — the context switch is charged per
  occurrence, not per minute — so the cheaper-looking item is the more
  expensive one once that is priced in.
- The second item is a far better automation candidate. Payback scales with
  occurrence count, so the same build effort pays back thirty times faster.
- The first item is a *scheduled* obligation, the second is *reactive* load.
  They need different interventions: one wants a calendar and an owner, the
  other wants code.

None of that is recoverable from "ten hours a year". So the schema requires
frequency and per-occurrence cost as **separate** fields and refuses an
aggregate total — `additionalProperties` is closed, so a hand-rolled
`hours_per_month` key fails validation rather than quietly becoming the number
everyone quotes. Every derived figure — annual load, payback period, rank — is
computed from those inputs at read time, which also means a corrected input
re-derives every figure that depended on it.

Both items above are in `registry/`. Run `tools/validate_registry.py --summary`
and the two lines that looked identical come out at **11 and 47 engineer-hours
a year**, a factor of four, entirely from the interrupt count and the share of
occurrences handled out of hours.

## A total is an interval

Handling time is right-skewed: most occurrences are routine and a few are
pathological. The registry therefore records a `p50` and a `p90` per item
rather than a mean, and derives the annual figure **twice** — once from each —
and reports the pair as a bracket.

This is deliberate, and the reason is not caution. Summing medians does not
produce the median of the sum, and for a skewed distribution a median-based
total *understates* the real load. A single number would be wrong with a
confident face; the bracket is wrong in a way the reader can see and argue
with. A reviewer who wants a point estimate is expected to pick an end of the
bracket and say which one they picked.

## Layout

```
schema/toil.schema.json     normative contract for a registry entry
registry/                   one file per inventory item, validated in CI
collectors/                 derive frequency and cost from operational exports
ranking/                    net saving, payback interval, and the ranked shortlist
tools/validate_registry.py  validation, invariants, and annual normalisation
```

## Quick start

```bash
python3 -m pip install -r requirements-dev.txt
python3 tools/validate_registry.py --summary
```

The validator does three separable jobs. It checks every entry against the
schema; it then checks the constraints a JSON Schema cannot express — that
`p90` is not below `p50`, that a filename matches the `id` inside it, that no
two entries claim the same id; and it derives the annual load. It exits
non-zero on any error, which is what the pipeline gates on. `--strict` promotes
warnings to failures, `--json` emits the same result for a machine, and
`--summary` prints the derived load so that a change to an input is visible as
a change to the number it drives.

One check is aimed at the tooling rather than the data: the validator
substitutes defaults for omitted cost fields, and it refuses to run if those
constants have drifted from the defaults the schema declares. A silent
divergence there would recost every entry that omits a field, and nothing
downstream would notice.

## How the annual figure is derived

```
occurrences          = count × periods_per_year
per_occurrence_min   = (handling_minutes + context_switch_minutes) × responders
annual_minutes       = occurrences × per_occurrence_min × out_of_hours_weight
out_of_hours_weight  = 1 + out_of_hours_fraction × (multiplier − 1)
```

Two of those terms are policy, not measurement, and the repository says so
rather than burying them:

- **The context switch is charged per responder per occurrence**, at a flat 15
  minutes. This is the term that makes a frequent, short item expensive, and it
  is the same for every entry so that entries stay comparable.
- **The out-of-hours multiplier is 1.5.** It asserts that a minute at 03:00
  costs half again what the same minute costs at 15:00, in recovery and in
  error rate. One number, declared once, for a reviewer to argue with.

Both are stated in the schema and asserted by the validator, so disagreeing
with them means changing them in one place and watching every derived figure
move.

## Where the numbers come from

Typing the numbers in by hand is how an estimate ends up labelled as a
measurement, so `collectors/` derives them from an export instead and fills the
window, the source and the basis from what the export actually contained:

```bash
python3 -m collectors.tickets \
  --export exports/ops-queue.ndjson --id stuck-queue-manual-drain \
  --from 2026-07-01 --to 2026-09-29 --source 'saved query: OPS toil' \
  --queue OPS --require-label toil --utc-offset-minutes 330 \
  --apply registry/stuck-queue-manual-drain.json
```

The collectors are where this repository's opinions about measurement live, and
each of them exists because the obvious reading of a source is wrong:

- **Resolution time is not handling time.** A ticket open for three days
  consumed twenty minutes of somebody's attention and three days of queue wait.
  Handling time comes from a worklog, or from first response to resolution, or
  from nothing — never from a ticket's lifetime unless that is asked for
  explicitly.
- **A firing is not an occurrence.** Flapping alerts are folded into single
  interruptions, and firings that cleared themselves without an
  acknowledgement are dropped, because they cost nobody anything and counting
  them raises the frequency while lowering the median.
- **A percentile needs a sample.** Below a stated sample size the upper end of
  the spread is reported as the observed maximum, and the basis is downgraded
  rather than the number being dressed up.

See [collectors/README.md](collectors/README.md) for the full reasoning and the
options. Collectors read files; they hold no credentials and open no network
connections, so a measurement is reproducible from the export behind it.

## Adding an item

Copy the closest existing entry in `registry/`, rename the file to the new
item's `id`, and keep these rules in mind — they are the ones the validator
enforces and the ones reviewers reject pull requests over:

1. **The filename stem must equal the `id`.** The registry is addressed by id;
   a mismatch makes an entry unfindable from a report that cites it.
2. **State a basis for every number.** `estimated` is an acceptable answer and
   a normal starting point. Claiming `measured` without a `window_days` and a
   `source` is rejected: a count without a window is not a rate.
3. **An item must be manual and repetitive to belong here.** One-off remedial
   work is an incident record, not toil, and filing it here inflates the
   inventory while making it less useful.
4. **`wont-automate` is a real, reviewable state.** It requires a written
   rationale and a decision date. An inventory where nothing can be closed
   stops being an inventory and becomes a backlog.

## Status

The schema, the validated inventory, the normalisation it supports, the
measurement collectors and the candidate ranking are in place.
Runbook-to-automation conversion lands next.

## Licence

MIT — see [LICENSE](LICENSE).
