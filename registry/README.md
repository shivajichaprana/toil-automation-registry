# registry/

One file per inventory item. The filename stem is the item's `id`, so
`registry/stuck-queue-manual-drain.json` holds the item whose `id` is
`stuck-queue-manual-drain`.

Entries are JSON rather than YAML on purpose: the normative contract in
`schema/` is a JSON Schema, and keeping the data in the schema's own format
means an editor with schema support validates an entry while it is being typed,
without a conversion step that could disagree with the pipeline.

## Conventions

- **Ids are stable.** Reports, dashboards and automation references cite the
  id. Rewording a `title` is free; changing an `id` breaks every citation, so
  retire an item instead and open a new one.
- **Numbers carry a basis.** `measured` means something counted it over a
  stated window and named the source. `sampled` means a subset was counted.
  `estimated` means an engineer guessed, which is a legitimate starting point
  and must be labelled as one.
- **Nothing is pre-multiplied.** Record occurrences and per-occurrence cost;
  the annual figure is derived. See the repository README for why.
- **Entries are reviewed, not archived.** `review.cadence_days` states how
  often an item must be revisited, and the validator warns once an entry is
  past due. An unreviewed inventory decays quietly into fiction.

The example entries committed here are synthetic. They exercise the full range
of the schema — including the branch where an item is deliberately *not* going
to be automated — so that a validation failure in the pipeline is a real signal
rather than a gap in coverage.
