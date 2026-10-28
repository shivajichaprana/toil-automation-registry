"""Measurement collectors for the toil inventory.

Each module in this package reads an operational export and produces the
``frequency`` and ``cost`` blocks of a registry entry, with the observation
window, the source and the basis derived from what the export actually
contained rather than typed in by hand.

Collectors read files. They hold no credentials and open no network
connections, so a measurement is reproducible from the export that produced it
and a checkout of this repository is not something that needs production
access.
"""

from __future__ import annotations

__all__ = ["alerts", "base", "tickets"]
