# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the pure trajectory math."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from atif_analytics.domain.trajectory import (
    MAX_WINDOWS_PER_CHUNK,
    WindowRow,
    build_row,
    chunk_windows,
    delta_value,
    format_chunk_xml,
    missing_keys,
    placeholder_row,
    xml_attr,
    xml_text,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _win(i: int, *, first: bool = False) -> WindowRow:
    prev = None if first else f"u-{i - 1}"
    return ("sid", prev, f"u-{i}", None if first else "user", "user", None, f"text {i}")


def test_max_windows_per_chunk_is_sixteen() -> None:
    assert MAX_WINDOWS_PER_CHUNK == 16


def test_chunk_windows_empty() -> None:
    assert chunk_windows([]) == []


def test_chunk_windows_splits_at_sixteen() -> None:
    windows = [_win(0, first=True)] + [_win(i) for i in range(1, 20)]
    chunks = chunk_windows(windows)
    assert [len(c) for c in chunks] == [16, 4]
    # Anchor sharing: chunk N's last curr equals chunk N+1's first prev.
    assert chunks[0][-1][2] == chunks[1][0][1]


def test_delta_value_table() -> None:
    # Every (prev, curr) sentiment pair, so no cell can drift unnoticed.
    assert delta_value("negative", "negative") == 0.0
    assert delta_value("negative", "neutral") == 1.0
    assert delta_value("negative", "positive") == 2.0
    assert delta_value("neutral", "negative") == -1.0
    assert delta_value("neutral", "neutral") == 0.0
    assert delta_value("neutral", "positive") == 1.0
    assert delta_value("positive", "negative") == -2.0
    assert delta_value("positive", "neutral") == -1.0
    assert delta_value("positive", "positive") == 0.0
    assert delta_value(None, "neutral") is None
    assert delta_value("neutral", None) is None


def test_format_chunk_xml_shape_and_truncation() -> None:
    long_text = "y" * 3000
    chunk: list[WindowRow] = [
        ("sid", None, "u-1", None, "user", None, "hello"),
        ("sid", "u-1", "u-2", "user", "user", long_text, "world"),
    ]
    xml = format_chunk_xml(chunk)
    assert "<window idx=0>" in xml
    assert '<prev role="" uuid="">' in xml  # session-first window
    assert '<curr role="user" uuid="u-1">hello</curr>' in xml
    assert "…(truncated)" in xml  # 3000 > default 2000 per-turn cap
    assert xml.count("<window") == 2


def test_xml_escaping() -> None:
    assert xml_attr('a"<b>&') == "a&quot;&lt;b&gt;&amp;"
    assert xml_text("<&>") == "&lt;&amp;&gt;"
    xml = format_chunk_xml([("sid", None, 'u"<1>', None, "user", None, "a <b> & c")])
    assert 'uuid="u&quot;&lt;1&gt;"' in xml
    assert "a &lt;b&gt; &amp; c" in xml


def test_placeholder_row_session_first_vs_mid() -> None:
    first = placeholder_row("sid", None, "u-1", NOW)
    assert first["prev_sentiment"] is None
    assert first["delta"] is None
    assert first["curr_sentiment"] == "neutral"
    assert first["transition_kind"] == "none"
    assert first["confidence"] == 0.0

    mid = placeholder_row("sid", "u-1", "u-2", NOW)
    assert mid["prev_sentiment"] == "neutral"
    assert mid["delta"] == 0.0


def test_build_row_trusts_model_delta_and_recomputes_on_garbage() -> None:
    win = {
        "prev_uuid": "u-1",
        "curr_uuid": "u-2",
        "prev_sentiment": "neutral",
        "curr_sentiment": "positive",
        "delta": 1,
        "is_transition": False,
        "transition_kind": "resolution",
        "confidence": 0.8,
    }
    row = build_row("sid", win, NOW)
    assert row["delta"] == 1.0
    assert row["transition_kind"] == "resolution"

    win_bad = {**win, "delta": "garbage", "transition_kind": "made-up-kind"}
    row_bad = build_row("sid", win_bad, NOW)
    assert row_bad["delta"] == 1.0  # recomputed from labels
    assert row_bad["transition_kind"] == "none"  # unknown kinds coerce to none


def test_missing_keys_detects_gaps() -> None:
    chunk: list[WindowRow] = [_win(0, first=True), _win(1), _win(2)]
    indexed: dict[tuple[str | None, str], dict[str, Any]] = {
        (None, "u-0"): {},
        ("u-1", "u-2"): {},
    }
    missing = missing_keys(chunk, indexed)
    assert [(m[1], m[2]) for m in missing] == [("u-0", "u-1")]
