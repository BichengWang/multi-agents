"""Validation shared by runner and benchmark entry points."""

import math


def validate_positive_finite(value: float, name: str) -> None:
    """Reject a configured limit that cannot bound work."""
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite, got {value!r}")
