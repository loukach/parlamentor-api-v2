"""Cost calculation for Anthropic API calls."""

from decimal import Decimal

# Pricing per million tokens (USD)
# Source: https://docs.anthropic.com/en/docs/about-claude/models
PRICING: dict[str, dict[str, Decimal]] = {
    "claude-opus-4-6": {
        "input": Decimal("15.00"),
        "output": Decimal("75.00"),
    },
    "claude-sonnet-4-5-20250929": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
    },
    "claude-sonnet-4-6": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
    },
    "claude-haiku-4-5-20251001": {
        "input": Decimal("0.80"),
        "output": Decimal("4.00"),
    },
}

# Cache pricing multipliers relative to input rate
CACHE_READ_MULTIPLIER = Decimal("0.1")
CACHE_CREATE_MULTIPLIER = Decimal("1.25")


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_create_tokens: int = 0,
) -> float:
    """Calculate USD cost from token counts.

    Returns 0.0 for unknown models (safe fallback).
    """
    prices = PRICING.get(model)
    if not prices:
        return 0.0

    million = Decimal("1_000_000")
    input_rate = prices["input"]
    output_rate = prices["output"]

    cost = (
        Decimal(input_tokens) * input_rate / million
        + Decimal(output_tokens) * output_rate / million
        + Decimal(cache_read_tokens) * input_rate * CACHE_READ_MULTIPLIER / million
        + Decimal(cache_create_tokens) * input_rate * CACHE_CREATE_MULTIPLIER / million
    )
    return float(cost)
