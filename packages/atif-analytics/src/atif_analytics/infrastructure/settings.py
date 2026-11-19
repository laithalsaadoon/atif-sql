# SPDX-License-Identifier: Apache-2.0

"""Env-driven settings for the analytics pipelines.

Pydantic v2 ``BaseSettings`` under the workspace ``ATIF_SQL_`` prefix
(atif-corpus / atif-models precedent). Carries the corpus root and the
per-pipeline knobs (friction char cutoff, batch size, budget ceilings,
transcript caps).

Composes :class:`atif_models.infrastructure.settings.LlmSettings` for the
LLM family/size/region/concurrency selection rather than duplicating those
fields.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from atif_analytics.domain.config import TranscriptCaps
from atif_analytics.domain.layout import AnalyticsLayout
from atif_analytics.domain.narration import (
    DEFAULT_RESULT_FAILED_TAIL_CHARS,
    DEFAULT_RESULT_HEAD_CHARS,
    DEFAULT_RESULT_TAIL_CHARS,
    DEFAULT_TOOL_INPUT_CHARS,
    DEFAULT_WINDOW_MAX_CHARS,
    DEFAULT_WINDOW_MIN_CHARS,
)

if TYPE_CHECKING:
    from atif_models.infrastructure.settings import LlmSettings


def _default_corpus_root() -> Path:
    """Mirror atif-corpus's default: ``~/.atif-sql/corpus/<slug of source root>``.

    Re-implements the slug locally (sha256 of the resolved source root,
    ``default`` for ``~/.claude``) because atif-analytics may not import
    atif-corpus (independence contract). The parity test pins both against
    the same expected strings.
    """
    import hashlib
    import re

    env_source = os.environ.get("ATIF_SQL_SOURCE_ROOT")
    if env_source is not None:
        source_root = Path(env_source)
    else:
        config_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
        source_root = config_root / "projects"
    resolved = source_root.expanduser().resolve()
    # corpus_slug parity: a bare ~/.claude source maps to the reserved
    # "default" key; everything else is <sanitized-dirname>-<8-hex sha256>.
    if resolved == Path("~/.claude").expanduser().resolve():
        slug = "default"
    else:
        name = re.sub(r"[^a-z0-9]+", "-", resolved.name.lower()).strip("-")[:32]
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
        slug = f"{name}-{digest}" if name else digest
    return Path.home() / ".atif-sql" / "corpus" / slug


class AnalyticsSettings(BaseSettings):
    """Env-driven configuration for the analytics pipelines."""

    model_config = SettingsConfigDict(
        env_prefix="ATIF_SQL_",
        env_file=".env",
        extra="ignore",
    )

    #: Materialized corpus root (the directory containing ``sessions/``).
    corpus_root: Path = Field(default_factory=_default_corpus_root)

    # --- LLM pipeline knobs ---
    #: Char cutoff for friction candidates: longer user turns are almost
    #: always genuine instructions.
    friction_max_chars: int = 300
    #: Hard per-run session ceiling for each LLM pipeline's candidate list
    #: (newest-first, so fresh sessions win). The unattended nightly cron
    #: relies on this: a backlogged corpus must never turn one tick into an
    #: unbounded spend. Env: ``ATIF_SQL_LLM_MAX_SESSIONS_PER_RUN``.
    llm_max_sessions_per_run: int = 50
    #: Hard per-run dollar ceiling across ALL LLM pipelines, checked against
    #: the UsageAccumulator's running actuals between stages — when crossed,
    #: the remaining LLM stages are aborted cleanly (nothing stamped for
    #: unstarted sessions). Env: ``ATIF_SQL_LLM_MAX_COST_USD_PER_RUN``.
    llm_max_cost_usd_per_run: float = 25.0
    #: Write chunking: rows land every ``max(batch_size * 4, 256)`` units.
    batch_size: int = 96
    #: Transcript caps: 800K per session, 50K per tool_result.
    session_text_total_max_chars: int = 800_000
    session_text_tool_result_max_chars: int = 50_000
    #: Send tool-result bodies to the model. Off: each result renders as a
    #: size stub (see TranscriptCaps). Env:
    #: ``ATIF_SQL_SESSION_TEXT_TOOL_RESULT_BODIES``.
    session_text_tool_result_bodies: bool = False

    # --- narrate pipeline knobs ---
    #: Ceiling of one narration window in rendered characters; a longer run
    #: of steps splits. Env: ``ATIF_SQL_NARRATE_WINDOW_MAX_CHARS``.
    narrate_window_max_chars: int = Field(default=DEFAULT_WINDOW_MAX_CHARS, ge=1)
    #: A window under this many rendered characters isn't closed at a
    #: boundary mark; it runs on into the next steps. Env:
    #: ``ATIF_SQL_NARRATE_WINDOW_MIN_CHARS``.
    narrate_window_min_chars: int = Field(default=DEFAULT_WINDOW_MIN_CHARS, ge=0)
    #: How many raw steps of the previous window each narration call carries
    #: over, and the character ceiling on them. Env:
    #: ``ATIF_SQL_NARRATE_CARRY_STEPS`` / ``ATIF_SQL_NARRATE_CARRY_MAX_CHARS``.
    narrate_carry_steps: int = Field(default=6, ge=0)
    narrate_carry_max_chars: int = Field(default=4_000, ge=0)
    #: How much of each tool result a narration call is shown: its opening
    #: and its ending, with a longer ending for a failed result. 0 for the
    #: head shows a result as its size only, the way the other pipelines
    #: read one. These are narrate's own bounds;
    #: ``session_text_tool_result_bodies`` doesn't apply to it. Env:
    #: ``ATIF_SQL_NARRATE_TOOL_RESULT_HEAD_CHARS`` /
    #: ``ATIF_SQL_NARRATE_TOOL_RESULT_TAIL_CHARS`` /
    #: ``ATIF_SQL_NARRATE_TOOL_RESULT_FAILED_TAIL_CHARS``.
    narrate_tool_result_head_chars: int = Field(default=DEFAULT_RESULT_HEAD_CHARS, ge=0)
    narrate_tool_result_tail_chars: int = Field(default=DEFAULT_RESULT_TAIL_CHARS, ge=0)
    narrate_tool_result_failed_tail_chars: int = Field(
        default=DEFAULT_RESULT_FAILED_TAIL_CHARS, ge=0
    )
    #: Length each tool call's arguments are shown at in a narration call
    #: (the other pipelines read 400). Env: ``ATIF_SQL_NARRATE_TOOL_INPUT_CHARS``.
    narrate_tool_input_chars: int = Field(default=DEFAULT_TOOL_INPUT_CHARS, ge=1)

    # --- review pipeline knobs ---
    #: What a review reads first. ``narration`` (the default) reads a
    #: session's story and window narrations, then opens the steps of the
    #: windows a triage call names; it needs the session narrated. ``steps``
    #: reads every step of the session with no narration and no triage call,
    #: which costs more and needs no narration. Env: ``ATIF_SQL_REVIEW_ROUTE``.
    review_route: Literal["narration", "steps"] = "narration"
    #: The most windows one session's review opens on the narration route.
    #: Env: ``ATIF_SQL_REVIEW_MAX_WINDOWS``.
    review_max_windows: int = Field(default=8, ge=1)
    #: Open a session's last main-chain window even when the triage call
    #: didn't name it: it holds the agent's closing report, where a claim the
    #: steps don't support is told to the reader. Env:
    #: ``ATIF_SQL_REVIEW_ALWAYS_LAST_WINDOW``.
    review_always_last_window: bool = True
    #: Ceiling of one review call's steps in rendered characters; more steps
    #: than that are split across calls at window ends. The default holds
    #: one full narration window and not two: a call packed with many
    #: windows attends less to each, and the faults in the middle of a long
    #: session are the ones it then misses. Env:
    #: ``ATIF_SQL_REVIEW_CALL_MAX_CHARS``.
    review_call_max_chars: int = Field(default=40_000, ge=1)
    #: How much of each tool result a review call is shown: its opening and
    #: its ending. A reviewer checks claims against results, so it reads more
    #: of one than a narration call does. Env:
    #: ``ATIF_SQL_REVIEW_TOOL_RESULT_HEAD_CHARS`` /
    #: ``ATIF_SQL_REVIEW_TOOL_RESULT_TAIL_CHARS``.
    review_tool_result_head_chars: int = Field(default=1_500, ge=1)
    review_tool_result_tail_chars: int = Field(default=1_500, ge=0)
    #: Length each tool call's arguments are shown at in a review call. Env:
    #: ``ATIF_SQL_REVIEW_TOOL_INPUT_CHARS``.
    review_tool_input_chars: int = Field(default=2_000, ge=1)
    #: A plain-text file saying what the operator wants from the agent and
    #: which problems matter most; at most 8,000 characters are read. It goes
    #: last in every review prompt and is part of the rubric digest on each
    #: row. Unset, the checklist stands alone. Env:
    #: ``ATIF_SQL_REVIEW_GOAL_FILE``.
    review_goal_file: Path | None = None
    #: Session kinds review leaves out, comma-separated. A turn audit is a
    #: reviewer's own session, so it's skipped by default; every other kind
    #: is read, one-shot jobs included. Env: ``ATIF_SQL_REVIEW_SKIP_KINDS``.
    review_skip_kinds: str = "turn_audit"

    # ------------------------------------------------------------------
    # Derivations
    # ------------------------------------------------------------------

    def layout(self) -> AnalyticsLayout:
        """The analytics artifact layout under :attr:`corpus_root`."""
        return AnalyticsLayout(corpus_root=self.corpus_root)

    def llm(self) -> LlmSettings:
        """The atif-models LLM settings (family/sizes/region/concurrency)."""
        from atif_models.infrastructure.settings import LlmSettings

        return LlmSettings()

    def transcript_caps(self) -> TranscriptCaps:
        """Project the transcript char caps."""
        return TranscriptCaps(
            session_text_total_max_chars=self.session_text_total_max_chars,
            session_text_tool_result_max_chars=self.session_text_tool_result_max_chars,
            session_text_tool_result_bodies=self.session_text_tool_result_bodies,
        )


__all__ = ["AnalyticsSettings"]
