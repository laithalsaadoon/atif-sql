# SPDX-License-Identifier: Apache-2.0

"""The converter-owned version of what a conversion produces.

The package version (``converter_version`` in ``meta.json``) moves with every
release, including ones that change nothing a corpus holds. This number moves
only when the artifacts a conversion writes change shape or meaning, so a
materialize pass can re-convert exactly the sessions an older converter wrote:
materialize stamps it into every ``meta.json`` as ``converter_schema`` and
treats a session recording a different value as stale, whether or not its
source moved. A source-removed session re-converts from its raw source archive.

THE RULE: bump this in the same commit as any change that can alter a byte of
any artifact for any input. A refactor that provably changes no output does not
bump it. ``packages/atif-converter/tests/test_converter_schema_version.py`` pins
a digest of the converter's code (docstrings and comments excluded) beside this
value, so any converter code change fails that test until someone decides:
bump if output can change, then re-pin the digest either way.

History:

* 1 — every converter before this constant existed (implicit).
* 2 — ``session_events.jsonl`` (hooks, injected context, API errors,
  compaction boundaries, cost-state, mode changes); ``records_captured`` in
  ``loss_report.json`` and the structural gaps reported only when enrichment
  didn't repair them; an unpriced model yields ``total_cost_usd`` NULL instead
  of $0; ``final_metrics.extra.reported_cost_usd`` from Claude Code's
  ``cost-state``; local price overrides for claude-opus-5-5 / claude-fable-5-1.
  In the same version: inline base64 attachments replaced by blob placeholders
  (and the bytes handed out for the corpus blob store); typed ``is_error`` /
  ``exit_code`` / ``interrupted`` / ``images`` on observation results;
  ``agent_id`` read from ``agentId`` and filled on every sidechain step;
  ``trajectory.extra.subagents`` from the ``agent-*.meta.json`` sidecars.
  Both change sets ship together and no corpus was ever stamped with only one
  of them, so one bump separates every version-1 session from every version-2
  one.
* 3 — harbor 0.23.0: every priced Claude Code step carries
  ``metrics.cost_usd`` and ``metrics.extra.cost_source`` (an unpriced step
  carries neither), and ``meta.json`` records ``harbor_version`` 0.23.0. Prices
  from litellm v1.102.0, which also prices claude-fable-5-1, so its local
  override retired and its sessions are labeled ``litellm_estimate``.
"""

from __future__ import annotations

from typing import Final

CONVERTER_SCHEMA_VERSION: Final = 3

__all__ = ["CONVERTER_SCHEMA_VERSION"]
