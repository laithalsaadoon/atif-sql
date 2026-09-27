# SPDX-License-Identifier: Apache-2.0

"""Per-pipeline config value-objects (pure, frozen dataclasses).

The transcript caps, carved into a small frozen dataclass so the renderer
never sees a model id or a corpus path. Stdlib-only; the env-driven
``AnalyticsSettings`` in ``infrastructure.settings`` projects down into it.
(The clustering, community and c-TF-IDF configs left with the structural
pipelines on 2026-09-27.)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TranscriptCaps:
    """Character caps for session-transcript assembly.

    The per-tool-result clip bounds arbitrarily large Bash / file-read
    outputs; the total cap keeps an assembled session under the model
    context window.
    """

    session_text_total_max_chars: int = 800_000
    session_text_tool_result_max_chars: int = 50_000


__all__ = ["TranscriptCaps"]
