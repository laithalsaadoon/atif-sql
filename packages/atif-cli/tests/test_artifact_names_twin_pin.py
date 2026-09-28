# SPDX-License-Identifier: Apache-2.0

"""Drift pin for the stored-artifact names every package spells on its own.

atif-corpus writes ``<name>.zst``; atif-duck, atif-analytics and atif-embed
read it, and the independence contract keeps each from importing the writer.
atif-cli may import them all, so the pin lives here: one name or suffix
drifting would leave a reader looking for a file the writer never writes.
"""

from __future__ import annotations

from atif_analytics.infrastructure import corpus_reader
from atif_corpus.domain import layout
from atif_duck.domain import artifacts
from atif_embed.infrastructure import corpus_text_rows


def test_the_suffix_matches_everywhere() -> None:
    assert (
        layout.COMPRESSED_SUFFIX
        == artifacts.COMPRESSED_SUFFIX
        == corpus_reader.COMPRESSED_SUFFIX
        == corpus_text_rows.COMPRESSED_SUFFIX
        == ".zst"
    )


def test_the_compressed_artifacts_match() -> None:
    assert layout.COMPRESSED_ARTIFACT_FILENAMES == artifacts.COMPRESSED_ARTIFACTS
    assert layout.TRAJECTORY_FILENAME == artifacts.TRAJECTORY_JSON
    assert layout.TRAJECTORY_FILENAME == corpus_reader.TRAJECTORY_FILENAME
    assert layout.TRAJECTORY_FILENAME == corpus_text_rows.TRAJECTORY_FILENAME
    assert layout.EDGES_FILENAME == artifacts.EDGES_JSONL == corpus_reader.EDGES_FILENAME
    assert layout.SESSION_EVENTS_FILENAME == artifacts.SESSION_EVENTS_JSONL
    assert layout.META_FILENAME == artifacts.META_JSON == corpus_reader.META_FILENAME
    assert layout.LOSS_REPORT_FILENAME == artifacts.LOSS_REPORT_JSON


def test_every_writer_name_is_what_the_readers_try_first() -> None:
    for name in layout.COMPRESSED_ARTIFACT_FILENAMES:
        assert layout.stored_filename(name) == artifacts.stored_names(name)[0]
