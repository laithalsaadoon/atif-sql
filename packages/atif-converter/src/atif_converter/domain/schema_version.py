# SPDX-License-Identifier: Apache-2.0

"""The converter-owned version of what a conversion produces.

The package version (``converter_version`` in ``meta.json``) moves with every
release, including ones that change nothing a corpus holds. This number moves
only when the artifacts a conversion writes change shape or meaning, so a
materialize pass can re-convert exactly the sessions an older converter wrote.
Nothing reads it yet on this branch; the corpus-side wiring (compare against
the value stamped at materialize time, re-convert on mismatch) is separate
work.

History:

* 1 — every converter before this constant existed (implicit).
* 2 — ``session_events.jsonl`` (hooks, injected context, API errors,
  compaction boundaries, cost-state, mode changes); ``records_captured`` in
  ``loss_report.json`` and the structural gaps reported only when enrichment
  didn't repair them; an unpriced model yields ``total_cost_usd`` NULL instead
  of $0; ``final_metrics.extra.reported_cost_usd`` from Claude Code's
  ``cost-state``; local price overrides for claude-opus-5-5 / claude-fable-5-1.
"""

from __future__ import annotations

from typing import Final

CONVERTER_SCHEMA_VERSION: Final = 2

__all__ = ["CONVERTER_SCHEMA_VERSION"]
