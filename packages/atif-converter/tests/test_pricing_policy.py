# SPDX-License-Identifier: Apache-2.0

"""The converter's pricing POLICY on top of the litellm-identical fast path.

``test_pricing_identity.py`` holds :func:`pricing.fast_cost_per_token` to
litellm bit for bit, including litellm's ``(0.0, 0.0)`` for a Claude id it
doesn't know. These tests pin what the converter does with that answer: an
unpriced model makes the session estimate ``None`` instead of $0, and the local
overrides price the two current models litellm 1.100.1 lacks.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from atif_converter.domain import pricing
from atif_converter.domain.claude_code_conversion import convert_claude_code_records

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

    def test_a_model_litellm_refuses_still_raises(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - whatever litellm raises
            _price("totally-unknown-model")

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

    def test_the_bundled_table_wins_once_it_holds_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = pricing.load_table()
        assert table is not None
        shadowing = pricing.PricingTable(
            path=table.path,
            entries={**table.entries, "claude-opus-5-5": table.entries["claude-opus-5"]},
            lowercase_keys={**table.lowercase_keys, "claude-opus-5-5": "claude-opus-5-5"},
            routing_rules=table.routing_rules,
            capability_rules=table.capability_rules,
        )
        monkeypatch.setattr(pricing, "_active_table", lambda: shadowing)
        assert pricing.local_override("claude-opus-5-5") is None
        # Priced from the (substituted) table entry now: claude-opus-5's $5 input.
        assert _price("claude-opus-5-5", (1_000_000, 0, 0, 0))[0] == pytest.approx(5.0)

    def test_overrides_are_absent_from_the_bundled_table(self) -> None:
        """GUARD: fails the day a litellm bump ships an overridden model.

        The bundled table then prices the key itself and the override is dead
        code that can drift from it. Delete the override (and its citation)
        when this fails.
        """
        table = pricing.load_table()
        assert table is not None
        shadowed = sorted(key for key in pricing.LOCAL_PRICE_OVERRIDES if key in table.entries)
        assert shadowed == [], f"litellm now bundles {shadowed}; delete their overrides"

    def test_overrides_carry_every_rate_the_arithmetic_reads(self) -> None:
        for model, entry in pricing.LOCAL_PRICE_OVERRIDES.items():
            for key in (
                "input_cost_per_token",
                "output_cost_per_token",
                "cache_creation_input_token_cost",
                "cache_read_input_token_cost",
            ):
                assert isinstance(entry.get(key), float), (model, key)
            assert entry["output_cost_per_token"] > entry["input_cost_per_token"]
            assert entry["cache_read_input_token_cost"] < entry["input_cost_per_token"]
        # JSON-serializable, like the table entries they stand in for.
        json.dumps(pricing.LOCAL_PRICE_OVERRIDES)
