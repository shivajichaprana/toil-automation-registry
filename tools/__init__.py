"""Entry-point scripts for the toil inventory.

The modules here are runnable as scripts and importable as a library. The
second property matters more than it looks: the annualisation arithmetic lives
in ``validate_registry`` and nowhere else, so the ranking package imports it
rather than restating it. A registry with two independent annualisers is a
registry whose two answers can drift apart without either one looking wrong.
"""

from __future__ import annotations

__all__ = ["validate_registry"]
