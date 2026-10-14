"""Per-model token prices for harnesses whose CLI/SDK reports tokens but not dollars.

Source: https://developers.openai.com/api/docs/pricing ("Standard" tier, USD per 1M tokens),
retrieved 2026-10-13. Only models listed on that page are priced; an unknown model has no price
and its cost is ``None`` -- a price is never guessed or borrowed from a similar model. Batch, flex
and fast tiers and regional-processing uplifts are not applied.

Claude Code reports its own ``total_cost_usd`` and is not priced here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Requests whose input exceeds this many tokens bill at the (higher) long-context rates.
LONG_CONTEXT_THRESHOLD = 272_000


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens. ``cache_write`` is None for models without a separate cache-write rate."""

    input: float
    cached_input: Optional[float]
    output: float
    cache_write: Optional[float] = None


# model -> (short context, long context or None)
_PRICES: dict[str, tuple[Price, Optional[Price]]] = {
    "gpt-6-astra": (Price(10.00, 1.00, 50.00, 12.50), Price(20.00, 2.00, 75.00, 25.00)),
    "gpt-6.1-sol": (Price(2.00, 0.10, 10.00, 2.50), Price(4.00, 0.20, 15.00, 5.00)),
    "gpt-6-luna": (Price(0.10, 0.01, 0.50, 0.125), Price(0.20, 0.02, 0.75, 0.25)),
    "gpt-6-sol": (Price(2.00, 0.20, 10.00, 2.50), Price(4.00, 0.40, 15.00, 5.00)),
    "gpt-5.6-sol": (Price(4.00, 0.40, 20.00, 5.00), Price(8.00, 0.80, 30.00, 10.00)),
    "gpt-5.6-terra": (Price(2.00, 0.20, 12.00, 2.50), Price(4.00, 0.40, 18.00, 5.00)),
    "gpt-5.6-luna": (Price(0.20, 0.02, 1.20, 0.25), Price(0.40, 0.04, 1.80, 0.50)),
    "gpt-5.5": (Price(5.00, 0.50, 30.00), Price(10.00, 1.00, 45.00)),
    "gpt-5.4": (Price(2.50, 0.25, 15.00), Price(5.00, 0.50, 22.50)),
    "gpt-5.4-mini": (Price(0.75, 0.075, 4.50), None),
    "gpt-5.4-nano": (Price(0.20, 0.02, 1.25), None),
    "gpt-5.2": (Price(1.75, 0.175, 14.00), None),
    "gpt-5.1": (Price(1.25, 0.125, 10.00), None),
    "gpt-5": (Price(1.25, 0.125, 10.00), None),
    "gpt-5-mini": (Price(0.25, 0.025, 2.00), None),
    "gpt-5-nano": (Price(0.05, 0.005, 0.40), None),
    "gpt-4.1": (Price(2.00, 0.50, 8.00), None),
    "gpt-4.1-mini": (Price(0.40, 0.10, 1.60), None),
    "gpt-4.1-nano": (Price(0.10, 0.025, 0.40), None),
    "gpt-4o": (Price(2.50, 1.25, 10.00), None),
    "gpt-4o-mini": (Price(0.15, 0.075, 0.60), None),
    "o1": (Price(15.00, 7.50, 60.00), None),
    "o3": (Price(2.00, 0.50, 8.00), None),
    "o3-mini": (Price(1.10, 0.55, 4.40), None),
    "o4-mini": (Price(1.10, 0.275, 4.40), None),
}

_SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def lookup_price(model: Optional[str], *, long_context: bool = False) -> Optional[Price]:
    """Price for ``model`` (dated snapshot suffixes ignored), or None when it is not listed."""
    if not model:
        return None
    name = _SNAPSHOT_SUFFIX.sub("", model.strip().lower())
    entry = _PRICES.get(name)
    if entry is None:
        return None
    short, long = entry
    return long if long_context and long is not None else short


def cost_usd(
    model: Optional[str],
    *,
    input_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int,
    long_context: bool = False,
) -> Optional[float]:
    """Dollar cost of token counts, or None when the model is unpriced.

    ``input_tokens`` includes cached and cache-write tokens (OpenAI convention); ``output_tokens``
    includes reasoning tokens.
    """
    price = lookup_price(model, long_context=long_context)
    if price is None:
        return None
    # Without a separate cache-write rate, written tokens bill as ordinary input.
    written = cache_write_tokens if price.cache_write is not None else 0
    cached_rate = price.cached_input if price.cached_input is not None else price.input
    fresh = max(input_tokens - cached_input_tokens - written, 0)
    total = (
        fresh * price.input
        + cached_input_tokens * cached_rate
        + written * (price.cache_write or 0.0)
        + output_tokens * price.output
    )
    return total / 1_000_000
