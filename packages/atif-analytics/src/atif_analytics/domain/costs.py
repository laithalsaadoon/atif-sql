# SPDX-License-Identifier: Apache-2.0

"""Pure cost arithmetic for the LLM-analytics pipelines.

:func:`estimate_cost_tokens` is the back-of-envelope dollar projection every
``--dry-run`` path uses to price a pending batch of classification calls. It
is pure arithmetic over token counts and a ``(input_rate, output_rate)``
$/MTok tuple — no DuckDB, no Bedrock, no settings — so it belongs in the
domain hexagon.

Distinct from :func:`atif_models.domain.registry.estimate_cost`, which prices
one ACCUMULATED usage total against a resolved ``ModelSpec``; this one prices
a PLANNED batch before any call is made. The dry-run plan dicts use this; the
post-run usage log uses the atif-models one.
"""

from __future__ import annotations

#: The chars→tokens heuristic every measured dry-run estimate uses. Four
#: chars per token is the standard English/code prior. Measuring the real
#: rendered length matters more than the ratio: a fixed per-call "avg
#: tokens" constant understates a real transcript by an order of magnitude.
CHARS_PER_TOKEN: int = 4


def tokens_for_chars(chars: int) -> int:
    """Estimate the token count of ``chars`` characters (chars/4, floor 1 when non-empty)."""
    if chars <= 0:
        return 0
    return max(1, chars // CHARS_PER_TOKEN)


def estimate_cost_tokens(
    input_tokens: int,
    output_tokens: int,
    pricing: tuple[float, float],
) -> float:
    """Dollar estimate for MEASURED token totals (the dry-run plan path).

    ``pricing`` is ``(input_rate, output_rate)`` in $/MTok. Cost is
    ``(in_tokens * in_rate + out_tokens * out_rate) / 1e6`` — a flat linear
    projection with no minimums, tiers, or cache accounting. Totals are
    summed per unit from real rendered prompt lengths, never from a static
    per-call average.
    """
    in_rate, out_rate = pricing
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


__all__ = ["CHARS_PER_TOKEN", "estimate_cost_tokens", "tokens_for_chars"]
