# SPDX-License-Identifier: Apache-2.0

"""Frozen slug values — the slug is an on-disk directory name.

``corpus_slug`` output is the ``<slug>`` in ``~/.atif-sql/corpus/<slug>/``, so
it is part of every installed user's filesystem layout. The concrete values
below are frozen (pinned 2026-08-22) to make a change to the algorithm fail
here instead of silently orphaning corpora already on disk.

A failure means FIX THE ALGORITHM, not the expectation. Updating an expected
value is only correct alongside a migration for existing corpora — see the
module docstring of ``atif_corpus.domain.slug``.

The input paths are hash inputs: editing one changes the digest it produces.
"""

from __future__ import annotations

from pathlib import Path

from atif_corpus.domain.slug import DEFAULT_CORPUS_KEY, corpus_slug


class TestFrozenSlugValues:
    def test_default_interactive_corpus_maps_to_reserved_key(self) -> None:
        assert corpus_slug("~/.claude") == DEFAULT_CORPUS_KEY
        assert corpus_slug(Path("~/.claude").expanduser()) == DEFAULT_CORPUS_KEY
        assert DEFAULT_CORPUS_KEY == "default"

    def test_concrete_values_are_frozen(self) -> None:
        """Exact directory names an installed corpus may already occupy."""
        assert corpus_slug("/nonexistent-atif-parity/alice/.claude/projects") == "projects-2079672d"
        assert corpus_slug("/nonexistent-atif-parity/My Corpus!!") == "my-corpus-3b7db9d1"
        assert corpus_slug("/nonexistent-atif-parity/team-a/.claude") == "claude-06b1b459"


class TestSlugProperties:
    def test_shared_dirname_roots_do_not_collide(self, tmp_path: Path) -> None:
        alice = tmp_path / "alice" / ".claude"
        bob = tmp_path / "bob" / ".claude"
        slug_a, slug_b = corpus_slug(alice), corpus_slug(bob)
        assert slug_a != slug_b
        assert slug_a.startswith("claude-")
        assert slug_b.startswith("claude-")

    def test_deterministic(self, tmp_path: Path) -> None:
        root = tmp_path / "some-corpus"
        assert corpus_slug(root) == corpus_slug(root)

    def test_dirname_with_no_slug_characters_falls_back_to_digest(self) -> None:
        slug = corpus_slug("/nonexistent-atif-parity/++++")
        assert len(slug) == 8
        assert all(c in "0123456789abcdef" for c in slug)
