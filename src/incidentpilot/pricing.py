"""Per-model token prices, so cost reports stay correct when the model changes.

USD per million tokens, Anthropic first-party list prices. A cache read bills at a tenth
of the input rate and a five-minute cache write at 1.25x; Claude Fable 5.1 reads are
priced separately.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


def _standard(input_rate: float, output_rate: float) -> Price:
    return Price(input_rate, output_rate, input_rate * 0.10, input_rate * 1.25)


PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.00, 50.00, 0.25, 12.50),
    "claude-opus-5": _standard(5.00, 25.00),
    "claude-opus-4-8": _standard(5.00, 25.00),
    "claude-sonnet-5": _standard(2.00, 10.00),
    "claude-sonnet-4-6": _standard(3.00, 15.00),
    "claude-haiku-4-5": _standard(1.00, 5.00),
}


def price_for(model: str) -> Price | None:
    return PRICES.get(model)


def cost_usd(model: str, usage: dict[str, int]) -> float | None:
    """Cost of a run's accumulated usage. None when the model's price is unknown -- an
    honest blank beats a confident number computed at somebody else's rates."""
    price = price_for(model)
    if price is None:
        return None
    return (
        usage.get("input_tokens", 0) / 1e6 * price.input
        + usage.get("cache_creation_input_tokens", 0) / 1e6 * price.cache_write
        + usage.get("cache_read_input_tokens", 0) / 1e6 * price.cache_read
        + usage.get("output_tokens", 0) / 1e6 * price.output
    )


def cache_hit_rate(usage: dict[str, int]) -> float | None:
    """Share of input tokens served from cache. None when there was no input."""
    total = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
             + usage.get("cache_creation_input_tokens", 0))
    if not total:
        return None
    return usage.get("cache_read_input_tokens", 0) / total
