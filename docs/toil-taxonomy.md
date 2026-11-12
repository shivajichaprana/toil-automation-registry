# Toil taxonomy

Record recurring manual work that maintains a service and leaves no durable
improvement after each occurrence. A person repeatedly identifying and
restarting the same stalled job is doing toil. Fixing the cause so the job can
recover by itself is engineering work that reduces that toil.

Give each entry one stable ID and one primary category from the
[registry schema](../schema/toil.schema.json). Choose the category by the work
performed. Use the trigger to describe how the work starts, and record
interruption, scale, and automation potential separately.

| Work performed | Category | Example |
| --- | --- | --- |
| Restore a queue or job | `queue-and-job-recovery` | Rerun a failed batch after verifying its checkpoint |
| Restore the credentials a service needs | `certificate-and-key-rotation` | Renew the same service certificate |
| Correct stored records | `data-correction` | Repair known malformed rows after validating affected IDs |
| Grant or review access | `access-management` | Grant temporary read-only access through an approved process |
| Collect evidence for a recurring review | `reporting-and-audit` | Assemble an access review evidence package |
| Determine what caused a recurring alert | `incident-triage` | Check the same dependency and route the case to its owner |

Split entries when the underlying steps, owner, or unit of measurement differ.
For example, investigating a lag alert and restoring the stalled job may be
separate work. If they are one inseparable procedure and the same timing
includes both, use one entry to avoid counting the same minutes twice.

At intake, confirm that a person performs the work, that it recurs, and that
one occurrence has a measurable boundary. Describe the trigger and the handoff
that ends the work. Name the team carrying the operational load. A recurring
task can qualify even if the team decides the cost or risk of automating it is
too high. Record that decision and its rationale in the entry.

Use the [measurement method](measurement-method.md) to estimate the load and
revisit the classification at the review cadence recorded in the entry.
