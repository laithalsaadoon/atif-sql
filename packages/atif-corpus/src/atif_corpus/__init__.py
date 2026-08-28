# SPDX-License-Identifier: Apache-2.0

"""atif-corpus: corpus materialization for ATIF trajectories.

Owns CONTRACT.md §Materialized corpus layout — session discovery over the
raw transcript corpus, per-session watermarks (convert only what changed),
quiescence detection (skip sessions still being written), and atomic
artifact writes. Hexagonal: ``domain`` (pure decisions) < ``infrastructure``
(fs/env effects) < ``application`` (the materialize use case). Conversion
happens behind :class:`atif_corpus.domain.ports.ConverterPort`; atif-cli
adapts the real converter (independence contract: this package never
imports atif-converter or atif-duck).
"""
