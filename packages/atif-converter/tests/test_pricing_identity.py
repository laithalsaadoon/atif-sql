# SPDX-License-Identifier: Apache-2.0

"""The pricing arithmetic returns the SAME floats as ``litellm.cost_per_token``.

litellm is a DEV dependency and is imported here and nowhere in ``src/``: the
module under test prices from the vendored table (``domain/model_prices.json``)
with its own copy of litellm's arithmetic, and every value it returns must be
``==`` (not approximately equal) to litellm's for the same data, or
``trajectory.json`` bytes would drift from what harbor wrote. The module is
skipped where litellm isn't installed; ``test_pricing_policy.py`` carries frozen
values for the same arithmetic that run everywhere.

The grid runs every bare vendored key whose entry is IDENTICAL to the one in
the installed litellm's bundled table (so a difference is arithmetic, never
data), crossed with token shapes that reach each branch of litellm's
arithmetic (no cache, cache heavier than the input count, inputs above the
128k / 200k / 272k / 512k thresholds), and with every service tier litellm
names plus the ``standard`` tier Claude Code reports. The corpus pairs are the
distinct ``(model, service_tier)`` values found in the two frozen benchmark
corpora on 2026-09-12.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any, cast

import pytest

from atif_converter.domain import pricing
from atif_converter.domain.pricing import (
    FastPathUnsupported,
    UnpriceableModelError,
    fast_cost_per_token,
)

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
litellm = pytest.importorskip("litellm")

#: Distinct (model, service_tier) pairs in the frozen Claude Code corpus.
CLAUDE_CORPUS_PAIRS: list[tuple[str, str | None]] = [
    ("claude-opus-5", "standard"),
    ("claude-fable-5", "standard"),
    ("claude-fable-5-1", "standard"),
    ("claude-opus-4-8", "standard"),
    ("claude-sonnet-5", "standard"),
    ("claude-haiku-4-5-20251001", "standard"),
    ("<synthetic>", None),
]

#: Distinct ``turn_context`` models in the frozen Codex corpus, as harbor's
#: lookup sees them: as given, then with the ``provider/`` prefix stripped.
CODEX_CORPUS_MODELS: list[str] = [
    "gpt-5.6-sol",
    "openai.gpt-5.6-sol",
    "openai.gpt-5.4",
    "openai.gpt-5.5",
    "bedrock-native/global.openai.gpt-6-astra",
    "global.openai.gpt-5.6-sol",
    "bedrock/global.openai.gpt-6-astra",
    "astra-native",
    "gpt-6-astra",
    "openai.gpt-6-astra",
    "global.openai.gpt-6-astra",
    "bedrock/global.openai.gpt-5.6-sol",
    "openai/gpt-5.1-codex",
]

#: (prompt, completion, cache_creation, cache_read) shapes reaching each branch.
TOKEN_SHAPES: list[tuple[int, int, int, int]] = [
    (0, 0, 0, 0),
    (5, 1, 0, 0),
    (1234, 567, 0, 0),
    (1234, 567, 8901, 23456),  # cache heavier than input: the double-counting branch
    (40507, 49534, 959267, 999110),  # the corpus maxima
    (130000, 10, 0, 0),  # above 128k
    (238085, 12582, 214679, 238085),  # above 200k, the Codex corpus maxima
    (300000, 100, 1000, 2000),  # above 272k
    (600000, 100, 1000, 2000),  # above 512k
]

SERVICE_TIERS: list[str | None] = [
    None,
    "standard",
    "flex",
    "priority",
    "fast",
    "ultrafast",
    "auto",
]

#: The share of vendored entries that must match the installed litellm's data
#: for the grid to mean anything. Below it the dev litellm and the table's
#: ``meta.ref`` have drifted apart; regenerate the table or move the dev pin.
MIN_SHARED_SHARE = 0.9


def _litellm_cost(
    model: str, shape: tuple[int, int, int, int], service_tier: str | None
) -> tuple[float, float] | type[Exception]:
    prompt, completion, creation, read = shape
    try:
        priced = litellm.cost_per_token(
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_creation_input_tokens=creation,
            cache_read_input_tokens=read,
            service_tier=service_tier,
        )
    except Exception as exc:  # noqa: BLE001, the oracle failing IS the expected value
        return type(exc)
    return cast("tuple[float, float]", priced)


def _fast_cost(
    model: str, shape: tuple[int, int, int, int], service_tier: str | None
) -> tuple[float, float] | type[Exception]:
    prompt, completion, creation, read = shape
    try:
        return fast_cost_per_token(
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_creation_input_tokens=creation,
            cache_read_input_tokens=read,
            service_tier=service_tier,
        )
    except UnpriceableModelError:
        return UnpriceableModelError


def _identical(ours: tuple[float, float], theirs: tuple[float, float]) -> bool:
    """Float identity, bit for bit: ``==`` plus matching types (no int 0 for float 0.0)."""
    return all(type(a) is type(b) and a == b for a, b in zip(ours, theirs, strict=True))


def _table() -> pricing.PricingTable:
    table = pricing.load_table()
    assert table is not None
    return table


def _vendored_bare_keys() -> list[str]:
    """Every bare key of the vendored table that carries litellm's data (no overrides)."""
    overrides = pricing.override_models()
    return sorted(k for k in _table().entries if "/" not in k and k not in overrides)


def _shared_keys() -> list[str]:
    """The vendored bare keys whose entry equals the installed litellm's bundled entry."""
    entries = _table().entries
    return [k for k in _vendored_bare_keys() if litellm.model_cost.get(k) == entries[k]]


class TestTable:
    def test_the_installed_litellm_carries_the_same_data(self) -> None:
        """The grid compares arithmetic over shared data, so most of the data must be shared."""
        vendored = _vendored_bare_keys()
        shared = _shared_keys()
        drifted = sorted(set(vendored) - set(shared))
        assert len(shared) >= MIN_SHARED_SHARE * len(vendored), (
            f"only {len(shared)}/{len(vendored)} vendored entries match "
            f"the installed litellm's bundled table (vendored ref {_table().source_ref}); "
            f"regenerate the table or move the dev pin. Drifted: {drifted[:20]}"
        )

    def test_has_pricing_entry_matches_model_cost_get(self) -> None:
        """For the corpus models, the vendored table holds exactly what litellm's does."""
        for model in CODEX_CORPUS_MODELS:
            for key in (model, model.split("/", 1)[-1]):
                assert pricing.has_pricing_entry(key) is bool(litellm.model_cost.get(key)), key


def _grid_cases() -> Iterator[tuple[str, tuple[int, int, int, int], str | None]]:
    for key in _shared_keys():
        for shape in TOKEN_SHAPES:
            for tier in SERVICE_TIERS:
                yield key, shape, tier


class TestIdentityGrid:
    def test_every_vendored_key_prices_identically(self) -> None:
        """For each shared key x shape x tier, ours == litellm's, and the fast path never declines.

        A decline is a model the table carries and the converter can't price,
        which since litellm stopped being a fallback means NULL cost; the
        filter in ``scripts/update_prices.py`` must not keep such an entry.
        """
        compared = 0
        declined: set[str] = set()
        mismatches: list[str] = []
        for key, shape, tier in _grid_cases():
            try:
                ours = _fast_cost(key, shape, tier)
            except FastPathUnsupported:
                declined.add(key)
                continue
            theirs = _litellm_cost(key, shape, tier)
            compared += 1
            if isinstance(ours, tuple) and isinstance(theirs, tuple):
                if not _identical(ours, theirs):
                    mismatches.append(f"{key} {shape} {tier}: ours={ours!r} litellm={theirs!r}")
            elif ours is not theirs:
                mismatches.append(f"{key} {shape} {tier}: ours={ours!r} litellm={theirs!r}")
        assert mismatches == [], "\n".join(mismatches[:40])
        assert declined == set(), sorted(declined)
        assert compared > 0
        print(f"grid: {compared} comparisons over {len(_shared_keys())} keys")

    @pytest.mark.parametrize(("model", "service_tier"), CLAUDE_CORPUS_PAIRS)
    @pytest.mark.parametrize("shape", TOKEN_SHAPES)
    def test_claude_corpus_pairs(
        self, model: str, service_tier: str | None, shape: tuple[int, int, int, int]
    ) -> None:
        if pricing.is_local_override(model):
            pytest.skip(f"{model} is priced from a local override, not litellm's data")
        ours = _fast_cost(model, shape, service_tier)  # must not decline
        theirs = _litellm_cost(model, shape, service_tier)
        if isinstance(ours, tuple):
            assert isinstance(theirs, tuple)
            assert _identical(ours, theirs), (ours, theirs)
        else:
            assert ours is UnpriceableModelError
            assert not isinstance(theirs, tuple)

    @pytest.mark.parametrize("model", CODEX_CORPUS_MODELS)
    @pytest.mark.parametrize("shape", TOKEN_SHAPES)
    def test_codex_corpus_models(self, model: str, shape: tuple[int, int, int, int]) -> None:
        """harbor prices the first of (model, stripped model) the table holds; ours == litellm's."""
        key = next(
            (k for k in (model, model.split("/", 1)[-1]) if bool(litellm.model_cost.get(k))), None
        )
        if key is None:
            assert not any(pricing.has_pricing_entry(k) for k in (model, model.split("/", 1)[-1]))
            return
        ours = _fast_cost(key, shape, None)
        theirs = _litellm_cost(key, shape, None)
        assert isinstance(ours, tuple)
        assert isinstance(theirs, tuple)
        assert _identical(ours, theirs), (key, ours, theirs)

    @pytest.mark.parametrize(
        "model",
        [
            "claude-test-1",
            "claude-newfamily-7",
            "claude-newfamily-7-2",
            "claude-sonnet-9-20301231",
            "totally-unknown-model",
            "<synthetic>",
        ],
    )
    def test_unmapped_names_agree_with_litellm(self, model: str) -> None:
        """Unmapped Claude ids price to litellm's (0.0, 0.0); other unknowns fail both sides."""
        shape = (1234, 567, 8901, 23456)
        theirs = _litellm_cost(model, shape, "standard")
        ours = _fast_cost(model, shape, "standard")
        if isinstance(theirs, tuple):
            assert isinstance(ours, tuple)
            assert _identical(ours, theirs), (model, ours, theirs)
        else:
            assert ours is UnpriceableModelError

    def test_a_case_variant_is_unpriced_where_litellm_prices_it(self) -> None:
        """The one known shape where dropping the litellm fallback loses a price.

        litellm resolves a model id whose case differs from its table key; the
        fast path declines it, and without the fallback that means no cost.
        No transcript has reported such an id, so this pins the gap rather
        than closing it.
        """
        shape = (1234, 567, 8901, 23456)
        assert isinstance(_litellm_cost("Claude-Opus-5", shape, "standard"), tuple)
        with pytest.raises(FastPathUnsupported):
            _fast_cost("Claude-Opus-5", shape, "standard")
        priced: Any = pricing.priced_cost_per_token(
            model="Claude-Opus-5",
            prompt_tokens=shape[0],
            completion_tokens=shape[1],
            cache_creation_input_tokens=shape[2],
            cache_read_input_tokens=shape[3],
            service_tier="standard",
        )
        assert priced is None
