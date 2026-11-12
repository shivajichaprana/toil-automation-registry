# Measurement method

Measure how often people perform the work and how long they are actively
engaged. Keep both inputs with their evidence so a reviewer can reproduce the
annual estimate and compare it with later measurements.

1. **Define an occurrence.** Name the service, the work that starts it, and
   the point where the responder stops handling it. Use the same definition
   before and after an automation change.
2. **Choose a representative window.** Record its dates, filters, timezone,
   and source. Note seasonality and any intervals excluded from the export.
   Reuse the same filtering and time rules when comparing windows.
3. **Count handled occurrences.** Group related firings for the same alert rule
   and target. Exclude brief self-resolving firings with no acknowledgement. For tickets,
   record human handling from worklogs where available. A ticket that opens
   inside the window and resolves afterwards can contribute to frequency while
   its incomplete timing stays out of the cost sample.
4. **Record the spread and basis.** Store the median and the upper estimate.
   When there are too few samples to support a percentile, the collectors use
   the observed maximum as the upper bound and cap the basis at `sampled`.
   Mark judgement-based inputs `estimated`. Keep frequency and cost evidence
   separate so uncertainty in one remains visible.
5. **Validate and rank.** Run `make check` and `make rank`. Compare the best
   and worst payback against the intended horizon. Confirm that the proposed
   automation still repays its build at the low end of savings before calling
   the case robust. Use the budget option in the [ranking CLI](../ranking/)
   when comparing a shortlist with a fixed build allocation.

The validator annualises the two ends of the handling spread separately. It
accounts for responders, context switching, and the share of occurrences that
fall outside business hours. See [the formula](../README.md#how-the-annual-figure-is-derived)
and [collector rules](../collectors/README.md) for the exact assumptions.

## Measure remaining manual work

Save a baseline before rollout. At the next review, count human interventions
using a comparable observation window and record their handling times.
Include exceptions sent back to an operator and manual recovery after failed
automation. Compare annualised low and high estimates side by side and record
changes in traffic or service scope that could explain the difference. If the
follow-up window has zero interventions, record the observation window and
source as evidence and repeat the measurement over a representative period.

For example, a queue recovery procedure with 20 human interventions before a
change and 5 after has 75% fewer interventions over those comparable windows.
If the remaining five are difficult cases, the reduction in handling hours can
be smaller. Record both counts and timings before updating the estimated share
of manual work left. Keep prior evidence so later reviews can tell sustained
improvement from a quiet period.
