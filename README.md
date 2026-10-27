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
aggregate total. Every derived figure — annual load, payback period, rank — is
computed from those inputs at read time, which also means a corrected input
re-derives every figure that depended on it.

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
schema/      normative JSON Schema for a registry entry
registry/    one file per inventory item, validated in CI
tools/       validation and normalisation entry points
```

## Status

Early. The repository scaffold is in place; the normative schema, the validated
inventory, measurement collectors, candidate ranking and runbook conversion
land next.

## Licence

MIT — see [LICENSE](LICENSE).
