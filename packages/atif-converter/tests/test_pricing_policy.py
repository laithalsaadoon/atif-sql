# SPDX-License-Identifier: Apache-2.0

"""The converter's pricing POLICY and the vendored table, with no litellm installed.

``test_pricing_identity.py`` holds :func:`pricing.fast_cost_per_token` to
litellm bit for bit where litellm is available (a dev dependency), including
litellm's ``(0.0, 0.0)`` for a Claude id it doesn't know. These tests run
everywhere and pin what the converter does with that answer: an unpriced model
makes the session estimate ``None`` instead of $0, the local overrides price the
two current models litellm 1.100.1 lacks, frozen values pin the arithmetic, and
pricing never imports litellm.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from atif_converter.domain import pricing
from atif_converter.domain.claude_code_conversion import convert_claude_code_records

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "update_prices.py"

#: (model, (prompt, completion, cache_creation, cache_read), tier, expected), captured
#: from ``litellm.cost_per_token`` 1.100.1 on 2026-09-27. They hold the arithmetic
#: still when litellm isn't installed to compare against.
FROZEN_PRICES: list[tuple[str, tuple[int, int, int, int], str, tuple[float, float]]] = [
    ("claude-opus-5", (1234, 567, 8901, 23456), "standard", (0.06735925, 0.014175)),
    ("claude-opus-5", (238085, 12582, 214679, 238085), "standard", (1.46078625, 0.31455)),
    ("claude-sonnet-5", (1234, 567, 8901, 23456), "standard", (0.0269437, 0.0056700000000000006)),
    (
        "claude-sonnet-5",
        (238085, 12582, 214679, 238085),
        "standard",
        (0.5843145000000001, 0.12582000000000002),
    ),
    (
        "claude-haiku-4-5-20251001",
        (1234, 567, 8901, 23456),
        "standard",
        (0.01347185, 0.0028350000000000003),
    ),
    (
        "claude-haiku-4-5-20251001",
        (238085, 12582, 214679, 238085),
        "standard",
        (0.29215725000000003, 0.06291000000000001),
    ),
    ("claude-opus-4-8", (1234, 567, 8901, 23456), "standard", (0.06735925, 0.014175)),
    ("claude-opus-4-8", (238085, 12582, 214679, 238085), "standard", (1.46078625, 0.31455)),
    (
        "global.anthropic.claude-opus-4-7",
        (1234, 567, 8901, 23456),
        "standard",
        (0.06735925, 0.014175),
    ),
    (
        "global.anthropic.claude-opus-4-7",
        (238085, 12582, 214679, 238085),
        "standard",
        (1.46078625, 0.31455),
    ),
    ("gpt-5.6-sol", (1234, 567, 8901, 23456), "standard", (0.0538874, 0.011340000000000001)),
    ("gpt-5.6-sol", (1234, 567, 8901, 23456), "priority", (0.1077748, 0.022680000000000002)),
    (
        "gpt-5.6-sol",
        (238085, 12582, 214679, 238085),
        "standard",
        (1.1686290000000001, 0.25164000000000003),
    ),
    (
        "gpt-5.6-sol",
        (238085, 12582, 214679, 238085),
        "priority",
        (2.3372580000000003, 0.5032800000000001),
    ),
    (
        "global.openai.gpt-5.6-sol",
        (1234, 567, 8901, 23456),
        "standard",
        (0.0538874, 0.011340000000000001),
    ),
    (
        "global.openai.gpt-5.6-sol",
        (1234, 567, 8901, 23456),
        "priority",
        (0.0538874, 0.011340000000000001),
    ),
    (
        "global.openai.gpt-5.6-sol",
        (238085, 12582, 214679, 238085),
        "standard",
        (1.1686290000000001, 0.25164000000000003),
    ),
    (
        "global.openai.gpt-5.6-sol",
        (238085, 12582, 214679, 238085),
        "priority",
        (1.1686290000000001, 0.25164000000000003),
    ),
    (
        "gpt-5.1-codex",
        (1234, 567, 8901, 23456),
        "standard",
        (0.0029319999999999997, 0.0056700000000000006),
    ),
    (
        "gpt-5.1-codex",
        (1234, 567, 8901, 23456),
        "priority",
        (0.005863999999999999, 0.011340000000000001),
    ),
    (
        "gpt-5.1-codex",
        (238085, 12582, 214679, 238085),
        "standard",
        (0.029760625, 0.12582000000000002),
    ),
    (
        "gpt-5.1-codex",
        (238085, 12582, 214679, 238085),
        "priority",
        (0.05952125, 0.25164000000000003),
    ),
    ("gpt-5.5", (1234, 567, 8901, 23456), "standard", (0.011727999999999999, 0.01701)),
    ("gpt-5.5", (1234, 567, 8901, 23456), "priority", (0.023455999999999998, 0.03402)),
    ("gpt-5.5", (238085, 12582, 214679, 238085), "standard", (0.1190425, 0.37746)),
    ("gpt-5.5", (238085, 12582, 214679, 238085), "priority", (0.238085, 0.75492)),
]

UNKNOWN_CLAUDE = "claude-newfamily-7"


def _price(model: str, shape: tuple[int, int, int, int] = (1000, 1000, 0, 0)) -> Any:
    prompt, completion, creation, read = shape
    return pricing.priced_cost_per_token(
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_creation_input_tokens=creation,
        cache_read_input_tokens=read,
        service_tier="standard",
    )


def _assistant(uuid: str, ts: str, model: str, *, msg_id: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "s1",
        "timestamp": f"2026-09-27T00:00:{ts}Z",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1000, "output_tokens": 1000},
        },
    }


def _user(uuid: str, ts: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": "s1",
        "timestamp": f"2026-09-27T00:00:{ts}Z",
        "message": {"role": "user", "content": "go"},
    }


def _final(records: list[dict[str, Any]]) -> dict[str, Any]:
    trajectory = convert_claude_code_records(records)
    assert trajectory is not None
    dumped = trajectory.model_dump(mode="json", exclude_none=True)
    return dumped["final_metrics"]


class TestUnpricedIsNone:
    def test_fast_path_still_says_zero_but_the_policy_says_unpriced(self) -> None:
        assert pricing.fast_cost_per_token(
            model=UNKNOWN_CLAUDE,
            prompt_tokens=1000,
            completion_tokens=1000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ) == (0.0, 0.0)
        assert _price(UNKNOWN_CLAUDE) is None

    def test_a_priced_model_is_exactly_the_litellm_price(self) -> None:
        shape = (1234, 567, 8901, 23456)
        expected = pricing.cost_per_token(
            model="claude-opus-5",
            prompt_tokens=shape[0],
            completion_tokens=shape[1],
            cache_creation_input_tokens=shape[2],
            cache_read_input_tokens=shape[3],
            service_tier="standard",
        )
        assert _price("claude-opus-5", shape) == expected

    def test_a_model_nobody_prices_is_none(self) -> None:
        assert _price("totally-unknown-model") is None
        assert _price("openai/gpt-5.1-codex") is None  # a provider prefix is not priced
        with pytest.raises(pricing.UnpriceableModelError):
            pricing.cost_per_token(
                model="totally-unknown-model",
                prompt_tokens=1,
                completion_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

    def test_session_of_an_unpriced_model_has_no_total(self) -> None:
        final = _final([_user("u1", "01"), _assistant("a1", "02", UNKNOWN_CLAUDE, msg_id="m1")])
        assert "total_cost_usd" not in final
        assert "cost_source" not in (final.get("extra") or {})

    def test_one_unpriced_step_makes_a_mixed_session_unpriced(self) -> None:
        """A priced haiku step beside an unpriced one is a partial sum, so it isn't reported."""
        final = _final(
            [
                _user("u1", "01"),
                _assistant("a1", "02", "claude-haiku-4-5-20251001", msg_id="m1"),
                _assistant("a2", "03", UNKNOWN_CLAUDE, msg_id="m2"),
            ]
        )
        assert "total_cost_usd" not in final


class TestLocalOverrides:
    def test_opus_5_5_is_priced_from_its_published_rates(self) -> None:
        # $4 / $20 per MTok, cache read $0.20, 5m cache write $5.
        prompt_cost, completion_cost = _price("claude-opus-5-5", (1_000_000, 1_000_000, 0, 0))
        assert prompt_cost == pytest.approx(4.0)
        assert completion_cost == pytest.approx(20.0)
        cached = _price("claude-opus-5-5", (3_000_000, 0, 1_000_000, 1_000_000))
        # 1M uncached input at $4 + 1M cache read at $0.20 + 1M cache write at $5.
        assert cached[0] == pytest.approx(4.0 + 0.2 + 5.0)

    def test_fable_5_1_is_priced_from_its_published_rates(self) -> None:
        prompt_cost, completion_cost = _price("claude-fable-5-1", (1_000_000, 1_000_000, 0, 0))
        assert prompt_cost == pytest.approx(10.0)
        assert completion_cost == pytest.approx(50.0)
        cached = _price("claude-fable-5-1", (1_000_000, 0, 0, 1_000_000))
        assert cached[0] == pytest.approx(0.25)

    def test_override_priced_session_is_labeled(self) -> None:
        final = _final([_user("u1", "01"), _assistant("a1", "02", "claude-opus-5-5", msg_id="m1")])
        assert final["total_cost_usd"] == pytest.approx(0.004 + 0.02)
        assert final["extra"]["cost_source"] == pricing.COST_SOURCE_WITH_OVERRIDES

    def test_table_priced_session_keeps_the_litellm_label(self) -> None:
        final = _final([_user("u1", "01"), _assistant("a1", "02", "claude-opus-5", msg_id="m1")])
        assert final["extra"]["cost_source"] == pricing.COST_SOURCE_LITELLM

    def test_overrides_carry_every_rate_the_arithmetic_reads(self) -> None:
        table = pricing.load_table()
        assert table is not None
        assert pricing.override_models() == {"claude-opus-5-5", "claude-fable-5-1"}
        for model in pricing.override_models():
            entry = table.entries[model]
            for key in (
                "input_cost_per_token",
                "output_cost_per_token",
                "cache_creation_input_token_cost",
                "cache_read_input_token_cost",
            ):
                assert isinstance(entry.get(key), float), (model, key)
            assert entry["output_cost_per_token"] > entry["input_cost_per_token"]
            assert entry["cache_read_input_token_cost"] < entry["input_cost_per_token"]
            sources = entry[pricing.OVERRIDE_MARKER]["sources"]
            assert sources, model
            assert all(url.startswith("https://") for url in sources), model


class TestFrozenPrices:
    @pytest.mark.parametrize(("model", "shape", "tier", "expected"), FROZEN_PRICES)
    def test_frozen_value(
        self,
        model: str,
        shape: tuple[int, int, int, int],
        tier: str,
        expected: tuple[float, float],
    ) -> None:
        prompt, completion, creation, read = shape
        assert (
            pricing.cost_per_token(
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cache_creation_input_tokens=creation,
                cache_read_input_tokens=read,
                service_tier=tier,
            )
            == expected
        )


class TestVendoredTable:
    def test_the_table_ships_beside_the_module_with_its_license(self) -> None:
        document = json.loads(pricing.TABLE_PATH.read_text(encoding="utf-8"))
        meta = document["meta"]
        assert meta["license"] == "MIT"
        assert "Copyright (c) 2023 Berri AI" in meta["license_text"]
        assert meta["source"].startswith("https://github.com/BerriAI/litellm/")
        table = pricing.load_table()
        assert table is not None
        assert meta["ref"] == table.source_ref
        assert len(meta["sha256"]) == 64

    def test_every_entry_is_a_covered_text_model(self) -> None:
        table = pricing.load_table()
        assert table is not None
        for key, entry in table.entries.items():
            assert entry["litellm_provider"] in {
                "anthropic",
                "openai",
                "bedrock",
                "bedrock_converse",
            }, key
            assert entry["mode"] in {"chat", "responses", "completion"}, key

    def test_pricing_never_imports_litellm(self) -> None:
        """A fresh interpreter with litellm made unimportable prices a corpus model."""
        code = (
            "import sys\n"
            "sys.modules['litellm'] = None\n"
            "from atif_converter.domain import pricing\n"
            "cost = pricing.cost_per_token(model='claude-opus-5', prompt_tokens=1234, "
            "completion_tokens=567, cache_creation_input_tokens=8901, "
            "cache_read_input_tokens=23456, service_tier='standard')\n"
            "print(cost)\n"
        )
        out = subprocess.run(  # noqa: S603, fixed interpreter and code string
            [sys.executable, "-c", code], check=True, capture_output=True, text=True
        )
        assert out.stdout.strip() == "(0.06735925, 0.014175)"

    def test_an_unreadable_table_leaves_costs_unpriced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pricing.load_table.cache_clear()
        monkeypatch.setattr(pricing, "TABLE_PATH", tmp_path / "missing.json")
        try:
            assert pricing.load_table() is None
            assert _price("claude-opus-5") is None
            assert pricing.has_pricing_entry("gpt-5.5") is False
            assert pricing.cost_source_label(["claude-opus-5-5"]) == pricing.COST_SOURCE_LITELLM
        finally:
            pricing.load_table.cache_clear()


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("update_prices", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestUpdateScript:
    """``scripts/update_prices.py``'s filter and override rules, offline."""

    def test_keep_filters_to_covered_text_models(self) -> None:
        script = _load_script()
        chat = {"litellm_provider": "anthropic", "mode": "chat"}
        assert script.keep("claude-opus-5", chat)
        assert script.keep("anthropic/claude-opus-5", chat)
        assert script.keep(
            "us.anthropic.claude-opus-4-6-v1", {**chat, "litellm_provider": "bedrock"}
        )
        assert script.keep("gpt-5.1-codex", {"litellm_provider": "openai", "mode": "responses"})
        assert script.keep(
            "global.openai.gpt-5.6-sol", {"litellm_provider": "bedrock_converse", "mode": "chat"}
        )
        assert not script.keep(
            "gpt-image-1", {"litellm_provider": "openai", "mode": "image_generation"}
        )
        assert not script.keep("gemini-3-pro", {"litellm_provider": "gemini", "mode": "chat"})
        assert not script.keep("vertex_ai/claude-opus-5", {**chat, "litellm_provider": "vertex_ai"})
        assert not script.keep(
            "bedrock/us-east-1/anthropic.claude-x", {**chat, "litellm_provider": "bedrock"}
        )
        assert not script.keep(
            "text-embedding-3-large", {"litellm_provider": "openai", "mode": "chat"}
        )

    def test_an_override_retires_once_upstream_prices_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        script = _load_script()
        upstream = {
            "fallback_generalizations": {"rules": []},
            "sample_spec": {"litellm_provider": "openai", "mode": "chat"},
            "claude-opus-5-5": {
                "litellm_provider": "anthropic",
                "mode": "chat",
                "input_cost_per_token": 1e-06,
            },
        }
        document = script.build(upstream, ref="test", digest="0" * 64, license_text="MIT License")
        models = document["models"]
        assert models["claude-opus-5-5"] == upstream["claude-opus-5-5"]
        assert script.OVERRIDE_MARKER in models["claude-fable-5-1"]
        assert "sample_spec" not in models
        assert "claude-opus-5-5" in capsys.readouterr().err

    def test_the_committed_table_is_what_the_script_builds(self) -> None:
        """Every override in the committed table is the script's current one, verbatim."""
        script = _load_script()
        table = pricing.load_table()
        assert table is not None
        for model in pricing.override_models():
            assert table.entries[model] == script.OVERRIDES[model], model
        assert set(script.OVERRIDES) == pricing.override_models()
