"""Cost calculation for Anthropic API calls.

Local estimate only — actual billing may differ due to thinking token
summarization in Claude 4 models. Use Anthropic Console for exact costs.
"""

from decimal import Decimal

# Pricing per million tokens (USD) — updated 2026-03-18
# Source: https://platform.claude.com/docs/en/about-claude/pricing
PRICING: dict[str, dict[str, Decimal]] = {
    "claude-opus-4-6": {
        "input": Decimal("5.00"),
        "output": Decimal("25.00"),
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
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
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
