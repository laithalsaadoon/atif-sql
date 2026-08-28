# SPDX-License-Identifier: Apache-2.0

"""Application layer: the materialize use case.

Orchestrates domain decisions (plan) with infrastructure effects (scan,
convert, write) behind the :class:`atif_corpus.domain.ports.ConverterPort`.
"""
