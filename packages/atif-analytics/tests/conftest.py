# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for atif-analytics tests (uniquely-named fixture module)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from analytics_fixtures import build_fixture_corpus
from loguru import logger

from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.settings import AnalyticsSettings


@pytest.fixture
def captured_logs() -> Iterator[list[str]]:
    """Collect loguru records emitted during the test.

    ``caplog`` cannot see them: loguru writes through its own sink registry and
    never touches the stdlib handler pytest hooks into, so a ``caplog``-based
    assertion on loguru output passes whether the log fired or not.
    """
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    """A two-session contract-shaped fixture corpus."""
    return build_fixture_corpus(tmp_path / "corpus")


@pytest.fixture
def reader(corpus_root: Path) -> CorpusReader:
    """A corpus reader over the fixture corpus (default caps)."""
    return CorpusReader(corpus_root)


@pytest.fixture
def settings(corpus_root: Path) -> AnalyticsSettings:
    """Settings pointed at the fixture corpus."""
    return AnalyticsSettings(corpus_root=corpus_root)
