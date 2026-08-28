# SPDX-License-Identifier: Apache-2.0

"""Friction regex + deterministic-stamp tiers over fixtures."""

from __future__ import annotations

from analytics_fixtures import SESSION_IDS

from atif_analytics.application.use_cases.friction import (
    candidate_messages,
    deterministic_stamps,
)
from atif_analytics.domain.friction import REGEX_BANK, regex_fast_path
from atif_analytics.infrastructure.corpus_reader import CorpusReader

# ---------------------------------------------------------------------------
# Regex bank: assert per-label hits AND that everything else falls through
# ---------------------------------------------------------------------------


def test_regex_bank_has_three_labels() -> None:
    assert [label for label, _ in REGEX_BANK] == ["status_ping", "interruption", "correction"]


def test_status_ping_hits() -> None:
    for text in (
        "how's it going?",
        "any update?",
        "status update please",
        "where are we at",
        "still working?",
        "what's your eta",
    ):
        assert regex_fast_path(text) == ("status_ping", 0.9), text


def test_interruption_hits() -> None:
    for text in ("wait a sec", "stop there", "hold on", "actually, do Y", "nvm", "never mind"):
        assert regex_fast_path(text) == ("interruption", 0.9), text


def test_correction_hits() -> None:
    for text in ("no, not that", "nope", "try again", "that's wrong", "wrong file"):
        assert regex_fast_path(text) == ("correction", 0.9), text


def test_ambiguous_falls_through() -> None:
    for text in (
        "status?",  # too short/ambiguous by design
        "what's the status column called?",
        "delete that file",
        "screenshot?",  # LLM territory
        "wait until tests pass",  # not an interruption trigger shape? it IS ^wait\s...
    ):
        result = regex_fast_path(text)
        if text == "wait until tests pass":
            # A leading "wait " IS an interruption trigger shape, so this
            # matches by design rather than by accident.
            assert result == ("interruption", 0.9)
        else:
            assert result is None, text


def test_empty_input() -> None:
    assert regex_fast_path("") is None


# ---------------------------------------------------------------------------
# Deterministic stamps over the fixture corpus
# ---------------------------------------------------------------------------


def _stamps(reader: CorpusReader) -> dict[str, tuple[str, float, str]]:
    steps = reader.load_steps(SESSION_IDS[0])
    candidates = candidate_messages(steps, max_chars=300)
    return deterministic_stamps(steps, {c[0] for c in candidates})


def test_rule1_repeated_message(reader: CorpusReader) -> None:
    stamps = _stamps(reader)
    # u-05 repeats u-01's body within 10 user turns.
    assert stamps["u-05"] == ("unmet_expectation", 0.85, "sql")
    # The FIRST occurrence is not stamped.
    assert "u-01" not in stamps


def test_rule2_short_imperative(reader: CorpusReader) -> None:
    stamps = _stamps(reader)
    assert stamps["u-06"] == ("correction", 0.9, "sql")


def test_rule3_question_after_error(reader: CorpusReader) -> None:
    stamps = _stamps(reader)
    # u-04 ("why is it failing?") immediately follows the error tool_result step.
    assert stamps["u-04"] == ("confusion", 0.85, "sql")


def test_candidates_exclude_markers_and_long_messages(reader: CorpusReader) -> None:
    steps = reader.load_steps(SESSION_IDS[0])
    candidates = candidate_messages(steps, max_chars=300)
    uuids = {c[0] for c in candidates}
    assert "u-marker" not in uuids  # system marker excluded
    assert "u-sc" not in uuids  # sidechain excluded
    assert "u-02" not in uuids  # assistant role excluded
    assert {"u-01", "u-04", "u-05", "u-06"} <= uuids
    # The char cutoff bites: only "undo that" (9 chars) survives max_chars=10.
    short = candidate_messages(steps, max_chars=10)
    assert {c[0] for c in short} == {"u-06"}
