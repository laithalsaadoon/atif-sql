# SPDX-License-Identifier: Apache-2.0

"""The pricing fast path returns the SAME floats as ``litellm.cost_per_token``.

litellm is imported here and only here on the conversion side: the module
under test must price the covered shapes without it, and every value it
returns must be ``==`` (not approximately equal) to litellm's, or
``trajectory.json`` bytes would change. The grid runs every bare key of the
bundled table whose provider the fast path covers, crossed with token shapes
that reach each branch of litellm's arithmetic (no cache, cache heavier than
the input count, inputs above the 128k / 200k / 272k / 512k thresholds), and
with every service tier litellm names plus the ``standard`` tier Claude Code
reports. The corpus pairs are the distinct ``(model, service_tier)`` values
found in the two frozen benchmark corpora on 2026-09-12.
"""

from __future__ import annotations

import os
import sys
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

COVERED_PROVIDERS = {"anthropic", "openai", "bedrock", "bedrock_converse"}


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


def _bare_covered_keys() -> list[str]:
    table = pricing.load_table()
    assert table is not None
    return sorted(
        key
        for key, entry in table.entries.items()
        if "/" not in key and entry.get("litellm_provider") in COVERED_PROVIDERS
    )


class TestTable:
    def test_bundled_table_is_located_without_importing_litellm(self) -> None:
        """A fresh interpreter prices a corpus model with ``litellm`` absent from ``sys.modules``."""
        import subprocess

        code = (
            "import sys\n"
            "from atif_converter.domain import pricing\n"
            "cost = pricing.cost_per_token(model='claude-opus-5', prompt_tokens=1234, "
            "completion_tokens=567, cache_creation_input_tokens=8901, "
            "cache_read_input_tokens=23456, service_tier='standard')\n"
            "print(cost, 'litellm' in sys.modules)\n"
        )
        out = subprocess.run(  # noqa: S603, fixed interpreter and code string
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
        )
        assert out.stdout.strip() == "(0.06735925, 0.014175) False"

    def test_table_matches_litellm_model_cost(self) -> None:
        """Our parsed entries are litellm's ``model_cost``: same keys, same values."""
        table = pricing.load_table()
        assert table is not None
        assert set(table.entries) == set(litellm.model_cost)
        assert all(table.entries[key] == litellm.model_cost[key] for key in table.entries)

    def test_has_pricing_entry_matches_model_cost_get(self) -> None:
        for model in CODEX_CORPUS_MODELS:
            for key in (model, model.split("/", 1)[-1]):
                assert pricing.has_pricing_entry(key) is bool(litellm.model_cost.get(key)), key

    def test_remote_table_setting_disables_the_fast_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "false")
        with pytest.raises(FastPathUnsupported):
            fast_cost_per_token(
                model="claude-opus-5",
                prompt_tokens=1,
                completion_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )


def _grid_cases() -> Iterator[tuple[str, tuple[int, int, int, int], str | None]]:
    for key in _bare_covered_keys():
        for shape in TOKEN_SHAPES:
            for tier in SERVICE_TIERS:
                yield key, shape, tier


class TestIdentityGrid:
    def test_every_covered_table_key_prices_identically(self) -> None:
        """For each bare covered key x shape x tier, ours == litellm's, or the fast path declines."""
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
        keys = _bare_covered_keys()
        # The fast path must actually cover the table, not decline its way to a pass.
        assert compared >= 0.95 * len(keys) * len(TOKEN_SHAPES) * len(SERVICE_TIERS), (
            f"compared={compared} declined={sorted(declined)}"
        )
        print(
            f"grid: {compared} comparisons over {len(keys) - len(declined)}/{len(keys)} keys, "
            f"declined={sorted(declined)}"
        )

    @pytest.mark.parametrize(("model", "service_tier"), CLAUDE_CORPUS_PAIRS)
    @pytest.mark.parametrize("shape", TOKEN_SHAPES)
    def test_claude_corpus_pairs(
        self, model: str, service_tier: str | None, shape: tuple[int, int, int, int]
    ) -> None:
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
            "claude-fable-5-1",
            "claude-newfamily-7",
            "claude-newfamily-7-2",
            "claude-sonnet-9-20301231",
            "Claude-Opus-5",
            "totally-unknown-model",
            "<synthetic>",
        ],
    )
    def test_unmapped_names_agree_with_litellm(self, model: str) -> None:
        """Unmapped Claude ids price to litellm's (0.0, 0.0); other unknowns fail both sides."""
        shape = (1234, 567, 8901, 23456)
        theirs = _litellm_cost(model, shape, "standard")
        try:
            ours = _fast_cost(model, shape, "standard")
        except FastPathUnsupported:
            # A case-variant of a real key: litellm prices it, the fast path hands it over.
            assert model == "Claude-Opus-5"
            full = pricing.cost_per_token(
                model=model,
                prompt_tokens=1234,
                completion_tokens=567,
                cache_creation_input_tokens=8901,
                cache_read_input_tokens=23456,
                service_tier="standard",
            )
            assert isinstance(theirs, tuple)
            assert _identical(full, theirs)
            return
        if isinstance(theirs, tuple):
            assert isinstance(ours, tuple)
            assert _identical(ours, theirs), (model, ours, theirs)
        else:
            assert ours is UnpriceableModelError


class TestFallback:
    def test_public_entry_point_falls_back_to_litellm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        def fake_cost_per_token(**kwargs: Any) -> tuple[float, float]:
            calls.append(kwargs)
            return (1.5, 2.5)

        monkeypatch.setattr(litellm, "cost_per_token", fake_cost_per_token)
        # "openai/gpt-5.1-codex" carries a slash, which the fast path declines.
        cost = pricing.cost_per_token(
            model="openai/gpt-5.1-codex",
            prompt_tokens=10,
            completion_tokens=2,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        assert cost == (1.5, 2.5)
        assert calls == [
            {
                "model": "openai/gpt-5.1-codex",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "service_tier": None,
            }
        ]

    def test_fallback_pins_the_bundled_table_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
        pricing._import_litellm()
        assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "true"

    def test_missing_litellm_surfaces_as_import_error_on_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "litellm", None)
        with pytest.raises(ImportError):
            pricing.cost_per_token(
                model="openai/gpt-5.1-codex",
                prompt_tokens=10,
                completion_tokens=2,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

    def test_locate_table_handles_missing_litellm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "litellm", None)
        assert pricing.locate_table() is None
