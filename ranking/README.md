# ranking

Two modules, one split: `payback` produces hours and dates, `score` applies
policy. The split is the point. A priority weight folded into an hours figure
produces a payback date for hours nobody ever spends, and once that has
happened there is no way to tell from the output whether a candidate leads
because it is large or because somebody weighted it.

| module       | answers                                                        | reads                             |
| ------------ | -------------------------------------------------------------- | --------------------------------- |
| `payback.py` | what would automating this return, and when does it repay      | `frequency`, `cost`, `automation` |
| `score.py`   | which one first, and does that order survive the weights       | `payback`, plus the policy fields |

Neither module annualises anything. Both read `annual_load` from
`tools/validate_registry.py`, so the repository has exactly one annualiser and
a change to it moves every derived figure at once.

```bash
python3 -m ranking.payback --summary
python3 -m ranking.payback --item stuck-queue-manual-drain
python3 -m ranking.score --budget-hours 250
python3 -m ranking.score --json
```

Both exit `0` clean, `1` on a finding at error level, `2` when they could not
run. `--strict` promotes warnings to failures.

## What payback corrects

**Saved hours are not the entry's hours.** Automation removes the share a
machine can take and hands the rest back as exceptions, review of automated
runs, and maintenance of the automation itself — `residual_fraction`, the least
reliable number in the entry. The saving is net of it, and the result is
published with the residual the answer would survive.

**Payback is an interval.** Build effort is a spread and so is handling time,
which gives four corners. The conventional figure — median build over median
saving — is not the middle of that interval, and the verdict is taken from the
*pessimistic* corner instead:

| verdict           | means                                                               |
| ----------------- | ------------------------------------------------------------------- |
| `robust`          | every corner repays inside the horizon; the decision does not depend on which estimate is right |
| `marginal`        | the optimistic corner repays and the pessimistic one does not — the band the single figure hides |
| `outside-horizon` | not even the optimistic corner repays                               |
| `no-saving`       | the residual leaves nothing to remove                               |

**Two break-even figures, because reviewers argue about two different things.**
`break_even_residual` is the residual share at which the pessimistic corner
stops repaying, and the headroom beside it is how wrong the residual estimate
may be before the verdict flips. `break_even_build_hours` is the largest build
estimate that still repays. A thin headroom is reported rather than hidden: a
verdict that turns on a five-point change in the least reliable field is a real
answer, and saying so lets a reviewer argue about the residual instead of about
the verdict.

## What score corrects

**The weights are written down.** They are in `WEIGHTS`, they sum to one, and
the sum is asserted at import. Every component is normalised to `[0, 1]` with
its direction stated, so none dominates by having the widest units.

| component     | weight | what it is                                                       |
| ------------- | -----: | ---------------------------------------------------------------- |
| `load`        |   0.35 | net annual hours removed, relative to the largest candidate here |
| `efficiency`  |   0.30 | hours returned per hour invested over the horizon                |
| `interruption`|   0.15 | interrupt-driven work and the out-of-hours share                 |
| `exposure`    |   0.10 | privileged access and blocked delivery                           |
| `tail`        |   0.10 | how far the ninetieth percentile sits above the median           |

`load` is relative to the field, so a score is comparable within one inventory
and not across two. That is deliberate: the question a shortlist answers is
which of *these* to do first.

**Confidence shrinks a score; it never adds to one.** The weaker of the
frequency and cost bases picks a multiplier applied after the weighted sum, so
an entry whose numbers were guessed cannot outrank a measured one on the
strength of its guesses. The basis ordering is the collectors' own, not a second
one. For an entry de-ranked this way the report's recommendation is to measure
it, not to argue about it.

**Staleness is reported and not scored.** An entry past its review cadence is
marked. Its score is untouched, because its numbers may be wrong in either
direction and shrinking the score would assume they are overstated — and the
entry most likely to have grown since anyone looked at it is the one that would
then be cut.

**The order is tested against the weights.** Each weight is swept up and down
by a declared jitter and renormalised, the order is recomputed for every
combination, and each candidate is published with the range of ranks it
occupied. A rank shown as `3 [2-7]` is not a rank.

Firmness and separation are different questions, and the sweep only answers the
first. Two candidates that are close *in the same direction* on every component
are ordered identically by every weighting, so the sweep calls them firm and is
right to. The score gap between adjacent candidates is therefore reported
separately, and a gap below `NEGLIGIBLE_SCORE_GAP` is called what it is: not an
order.

## Why a ranking is not a plan

Build capacity is finite, so `--budget-hours` fits a shortlist by descending
return per build hour — the order that is optimal when effort can be split and
only near-optimal when it cannot. The loss is measured rather than assumed
away: the best affordable combination is computed by subset enumeration and the
gap is published. Above `EXACT_SELECTION_MAX_ITEMS` candidates the enumeration
is skipped and the report says the gap is unmeasured rather than zero.

Only `robust` candidates may consume budget. A marginal one can be argued for,
but not silently by a budget calculation, so it is listed as skipped with its
verdict rather than dropped.

## Declared policy constants

Each is a policy claim, not a measurement, and each is stated once so that a
reviewer has one number to argue with.

| constant                     |  value | claim                                                        |
| ---------------------------- | -----: | ------------------------------------------------------------ |
| `DEFAULT_HORIZON_MONTHS`     |     36 | years of automation life credited to a build                 |
| `MIN_HORIZON_MONTHS`         |     12 | below a year a seasonal rate has not completed a cycle       |
| `REFERENCE_RETURN`           |    5.0 | return per build hour treated as the top of the scale        |
| `REFERENCE_TAIL`             |    4.0 | a p90 four times the p50 is as tail-heavy as the scale goes  |
| `DEFAULT_JITTER`             |   0.25 | a disagreement about the weights a reviewer could plausibly have |
| `NEGLIGIBLE_SCORE_GAP`       |   0.01 | below this, two adjacent candidates are not ordered          |
| `EXACT_SELECTION_MAX_ITEMS`  |     18 | above this the exact selection is not attempted              |

The horizon fails in one direction, stated here rather than left implicit: an
automation rewritten or abandoned before the horizon returns less than it is
credited with, so a candidate repaying near the horizon is a weaker claim than
one repaying in a quarter even though both are inside it.

## What is not modelled

- **Hours already spent.** The schema records no progress against a build, so
  an `in-progress` entry is assessed with its original estimate. That
  overstates what remains and ranks the entry lower than it deserves; the
  direction is reported as a finding rather than corrected by a guess.
- **Discounting.** No rate is applied. A stated horizon with no discount is a
  declared policy; a discount rate would dress an unknown up as arithmetic.
- **Dependencies between candidates.** Two automations that share a platform
  may be cheaper together than apart. The selection treats build costs as
  independent and so overstates the budget two related candidates need.
