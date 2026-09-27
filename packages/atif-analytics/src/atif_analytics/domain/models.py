# SPDX-License-Identifier: Apache-2.0

"""Pydantic v2 classification schemas (pure domain models).

Enum value sets, field names, bounds, and descriptions are FROZEN by
CONTRACT-V2 §Ports & state. The descriptions are part of the prompt surface
— the strict-schema transform in atif-models ships them to the model — so
editing one here changes classifier behavior, and changing an enum value
breaks the analytics views bound to it.

The OpenAI strict-mode wire transform lives in
:mod:`atif_models.domain.schema`; these models stay pure pydantic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SessionClassification(BaseModel):
    """Classify an entire Claude Code session: what kind of work, toward what goal.

    One row per session, written to the ``session_classifications`` parquet
    cache. Used to build the ``session_classifications`` DuckDB view.

    ``autonomy_tier`` and ``success`` were removed on 2026-09-27: neither was
    reproducible, and what a session achieved is now computed from its steps
    (atif-duck's ``session_outcomes``).
    """

    model_config = ConfigDict(extra="forbid")

    work_category: Literal[
        "sde",
        "admin",
        "strategy_business",
        "events",
        "thought_leadership",
        "other",
    ] = Field(
        ...,
        description=(
            "Dominant activity.  "
            "'sde': software engineering, debugging, code review, tests, CI. "
            "'admin': expense reports, scheduling, low-signal email triage, routine ops. "
            "'strategy_business': business analysis, competitive research, strategic memos, "
            "proposals. "
            "'events': event planning/logistics, speaker prep, agenda building. "
            "'thought_leadership': writing for external audiences (blog posts, conference "
            "abstracts, LinkedIn). "
            "'other': use only when nothing else fits."
        ),
    )
    goal: str = Field(
        ...,
        min_length=1,
        max_length=280,
        description=(
            "ONE sentence summarizing the user's goal, inferred from the opening user "
            "messages and overall arc.  Present tense, <= 280 chars.  Example: "
            '"Refactor the auth middleware to use the new token rotator."'
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Classifier self-assessed confidence 0.0-1.0. Use <0.5 for genuinely "
            "ambiguous sessions."
        ),
    )


class ConflictPair(BaseModel):
    """A single stance-conflict pair, keyed on the two opposing turn UUIDs."""

    model_config = ConfigDict(extra="forbid")

    turn_a_uuid: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "UUID of the first turn that holds stance A.  Pull from the "
            "``[uuid=...]`` headers in the bound transcript -- copy verbatim, "
            "do NOT invent or paraphrase.  Together with ``turn_b_uuid`` this "
            "is the natural key of a conflict row."
        ),
    )
    turn_b_uuid: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "UUID of the second turn that holds the opposing stance.  Same "
            "rules as ``turn_a_uuid`` -- copy verbatim from the transcript "
            "headers.  Must differ from ``turn_a_uuid``."
        ),
    )
    conflict_kind: Literal["disagreement", "correction", "reversal", "impasse"] = Field(
        ...,
        description=(
            "Shape of the conflict.  "
            "'disagreement': two parties hold opposing positions and discuss them. "
            "'correction': one party explicitly tells the other their answer/action "
            "was wrong (e.g. 'no, not that, do X instead'). "
            "'reversal': the same party flips their own earlier position "
            "('actually let's NOT do X'). "
            "'impasse': both sides restate their positions without converging "
            "and the topic stalls."
        ),
    )
    severity: Literal["low", "medium", "high"] = Field(
        ...,
        description=(
            "How consequential the conflict is for the session outcome.  "
            "'low': a minor course nudge with little downstream impact. "
            "'medium': changes the implementation approach or scope but stays "
            "inside the original goal. "
            "'high': blocks progress, reverses a major decision, or "
            "fundamentally changes the goal."
        ),
    )
    agent_position: str = Field(
        ...,
        min_length=1,
        max_length=280,
        description=(
            "One-sentence summary of the agent's stance in this conflict, "
            "phrased in the agent's own framing.  Strip pleasantries; keep "
            "the substantive claim."
        ),
    )
    user_position: str = Field(
        ...,
        min_length=1,
        max_length=280,
        description=(
            "One-sentence summary of the user's stance, phrased the way the "
            "user phrased it.  Same length budget as ``agent_position``."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Classifier self-confidence 0.0-1.0 that this is a real, "
            "substantive conflict (not deliberation, not collaboration, not "
            "an accepted-risk caveat).  Use <0.5 for borderline cases."
        ),
    )


class ConflictsResult(BaseModel):
    """The model's response: zero or more conflict pairs.

    An empty list is valid and common.  When the model returns an empty
    list the pipeline writes ZERO rows for that session — sessions with no
    conflicts simply don't appear in ``session_conflicts`` (or in the
    derived ``conflicts_summary`` view).
    """

    model_config = ConfigDict(extra="forbid")

    conflicts: list[ConflictPair] = Field(
        default_factory=list,
        description=(
            "Zero or more stance-conflict pairs.  Each entry names the two "
            "turn UUIDs whose stances clash, the kind/severity, both party "
            "positions, and a confidence score.  Report only substantive "
            "technical or strategic conflicts -- skip trivial style "
            "disagreements."
        ),
    )


class UserFrictionSignal(BaseModel):
    """Classify a single short user message for friction signals.

    A "friction signal" is anything in the user's utterance that implies the
    agent's last turn fell short of expectations: an impatient status ping,
    a one-word question pointing at a missed artifact (``screenshot?``,
    ``tests?``), confusion about what happened, a hard interruption, a
    correction, or open frustration.

    Applied only to user-role messages below the friction char cutoff
    (default 300).  Long user messages are almost always genuine task turns
    rather than interrupt/confusion signals — the filter keeps LLM cost
    linear in the interesting slice.
    """

    model_config = ConfigDict(extra="forbid")

    label: Literal[
        "status_ping",
        "unmet_expectation",
        "confusion",
        "interruption",
        "correction",
        "frustration",
        "none",
    ] = Field(
        ...,
        description=(
            "Dominant friction category for this single user message.  "
            "'status_ping': the user asks about progress/ETA ('how's it going?', "
            "'any update?', 'status?', 'where are we?'). "
            "'unmet_expectation': a one- or two-word question that points at "
            "something the agent should have done proactively but didn't "
            "('screenshot?', 'tests?', 'link?', 'diff?'). "
            "'confusion': the user signals they don't understand the output or "
            "state ('what does that mean?', 'why did you do X?', 'I don't get it'). "
            "'interruption': the user cuts the agent off or redirects mid-task "
            "('wait', 'stop', 'hold on', 'actually...', 'before you do that'). "
            "'correction': the user tells the agent its last action/answer was "
            "wrong ('no, not that', 'that's wrong', 'nope', 'try again'). "
            "'frustration': terse annoyance or sarcasm ('ugh', 'seriously?', "
            "'are you kidding', 'really?'). "
            "'none': ordinary task turn with no friction signal -- a substantive "
            "instruction, a plain question, an acknowledgement. USE THIS FOR "
            "THE MAJORITY OF MESSAGES."
        ),
    )
    rationale: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "One short sentence (<=200 chars) naming the specific phrase or "
            "structural cue that triggered the label.  Use an empty-ish "
            "placeholder like 'ordinary instruction' when label='none'."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Classifier self-confidence 0.0-1.0.  Use <0.5 when the message is "
            "genuinely ambiguous between 'none' and a friction label."
        ),
    )


class PerceivedError(BaseModel):
    """One user-perceived agent error, keyed on the USER turn where it surfaces.

    Implements LangSmith's published "Perceived Error" definition —
    conversations where the agent "made a mistake, misunderstood a request,
    or took the interaction in the wrong direction", judged from
    USER-VISIBLE evidence only, never objective correctness. Explicit
    signals (correction / repeated_request / rejected_action) come from the
    user's own words; inferred signals (contradictory_response /
    acknowledged_mistake / persistent_misunderstanding / unresolved_outcome)
    come from the conversation shape.
    """

    model_config = ConfigDict(extra="forbid")

    turn_uuid: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "UUID of the USER turn where the perception of error surfaces "
            "(the correction, the repeat, the rejection, or the user-visible "
            "cue an inferred signal rests on).  Pull from the ``[uuid=...]`` "
            "headers in the bound transcript -- copy verbatim, do NOT invent "
            "or paraphrase.  For inferred signals with no single user cue "
            "turn, use the user turn closest AFTER the agent's error."
        ),
    )
    signal: Literal[
        "correction",
        "repeated_request",
        "rejected_action",
        "contradictory_response",
        "acknowledged_mistake",
        "persistent_misunderstanding",
        "unresolved_outcome",
    ] = Field(
        ...,
        description=(
            "The evidence category.  EXPLICIT (the user says so): "
            "'correction': the user tells the agent its answer/action was "
            "wrong or restates what they actually meant. "
            "'repeated_request': the user asks for the same thing again "
            "because the first response missed it. "
            "'rejected_action': the user declines, reverts, or interrupts "
            "an action the agent took or proposed. "
            "INFERRED (the conversation shows it): "
            "'contradictory_response': the agent contradicts its own "
            "earlier statement and the user is exposed to both. "
            "'acknowledged_mistake': the agent itself admits an error "
            "('you're right, I misread...') in front of the user. "
            "'persistent_misunderstanding': the agent keeps answering a "
            "different question than the one the user is asking across "
            "multiple turns. "
            "'unresolved_outcome': the session ends with the user's problem "
            "visibly unsolved after the agent's attempts."
        ),
    )
    severity: Literal["minor", "moderate", "major"] = Field(
        ...,
        description=(
            "How much the perceived error cost the user.  "
            "'minor': a one-turn nudge; the user corrected and work resumed "
            "immediately (the common case in coding sessions). "
            "'moderate': the error cost visible rework, repeated exchanges, "
            "or an undone/redone action. "
            "'major': the error derailed the session -- wrong direction "
            "sustained across many turns, destructive action rejected too "
            "late, or the user gave up."
        ),
    )
    evidence: str = Field(
        ...,
        min_length=1,
        max_length=280,
        description=(
            "The user's own words that show the perception -- a short "
            "verbatim quote or close paraphrase of the user turn(s), "
            "<= 280 chars.  For inferred signals, quote the user-visible "
            "text the inference rests on (e.g. the agent's own admission)."
        ),
    )
    agent_error_summary: str = Field(
        ...,
        min_length=1,
        max_length=280,
        description=(
            "One sentence describing what the agent got wrong FROM THE "
            "USER'S PERSPECTIVE, <= 280 chars.  Example: 'The agent renamed "
            "the CLI flag the user had asked to keep.'  Describe the "
            "perceived mistake, not whether it was objectively wrong."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Classifier self-confidence 0.0-1.0 that the USER perceived "
            "an error (not that an error objectively occurred).  Use <0.5 "
            "when the cue could equally be the user changing their mind or "
            "ordinary iterative steering."
        ),
    )


class PerceivedErrorsResult(BaseModel):
    """The model's response: zero or more perceived errors for one session.

    An empty list is valid and common — it means a CLEAN session (the user
    never perceived an error).  The pipeline writes ZERO rows for that
    session; the session is checkpointed so it never re-bills.
    """

    model_config = ConfigDict(extra="forbid")

    errors: list[PerceivedError] = Field(
        default_factory=list,
        description=(
            "Zero or more user-perceived agent errors.  Each entry names "
            "the user turn where perception surfaces, the evidence "
            "category, severity, the user's words, a one-sentence summary "
            "of the perceived mistake, and a confidence score.  Report "
            "only errors the USER visibly perceived -- an empty array is "
            "the correct answer for a clean session."
        ),
    )


__all__ = [
    "ConflictPair",
    "ConflictsResult",
    "PerceivedError",
    "PerceivedErrorsResult",
    "SessionClassification",
    "UserFrictionSignal",
]
