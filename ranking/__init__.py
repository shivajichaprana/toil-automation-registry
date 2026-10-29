"""Ranking of automation candidates against the toil inventory.

The package has a deliberate split, and it is the same split the repository
uses between collection and derivation:

``payback``
    Hours and dates only. It converts an entry's annual load into the load an
    automation would actually remove, works out when that removal pays for the
    build, and says how wrong the inputs may be before the answer changes. It
    applies no policy weights, because a priority weight folded into an hours
    figure produces a payback date for hours nobody ever spends.

``score``
    Policy only. It takes the hours from ``payback``, applies declared weights
    to the qualities the hours do not capture, and reports how stable the
    resulting order is under a plausible change of those weights.

Neither module annualises anything itself: both read the derivation in
``tools.validate_registry``, so the registry has exactly one annualiser.
"""

from __future__ import annotations

__all__ = ["payback", "score"]
