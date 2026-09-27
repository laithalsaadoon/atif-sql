# SPDX-License-Identifier: Apache-2.0

"""Static view/macro catalog for the atif-duck DuckDB surface.

Why a static catalog, not runtime introspection?
------------------------------------------------
``DESCRIBE`` over every view re-binds the raw trajectory reader and re-runs
JSON work per call, which puts the schema dump on the same cost curve as the
corpus size. Answering from this module instead keeps the agent-facing
``atif-sql schema`` command under 50 ms with zero DuckDB connection cost. The
price is that this file can lie, so drift against the actual DDL is caught by
two CI tests in ``tests/test_duck_views.py``:

* ``test_view_schema_matches_describe`` — registers the views over a
  synthetic contract-shaped fixture corpus and asserts ``DESCRIBE`` output
  equals :data:`VIEW_SCHEMA` column-for-column.
* ``test_macro_signatures_match_ddl`` — regex-parses ``CREATE OR REPLACE
  MACRO <name>(<args>)`` out of the registry source and asserts equality
  with :data:`MACRO_SIGNATURES`.
"""

from __future__ import annotations

# Business-level views emitted by ``atif_duck.infrastructure.registry``.
# The raw readers (``v_raw_trajectories``, ``v_raw_edges``,
# ``v_raw_loss_reports``, ``v_raw_meta``) are deliberately absent: they
# describe the corpus files rather than being queryable business surface. A
# ``v_raw_*`` name added here would appear in ``atif-sql schema`` and in the
# derived examples, promising a stable surface the registry does not offer.
VIEW_NAMES: tuple[str, ...] = (
    "sessions",
    "steps",
    "messages",
    "tool_calls",
    "tool_results",
    "todo_events",
    "todo_state_current",
    "subagent_spawns",
    "task_creations",
    "task_updates",
    "tasks_state_current",
    "skill_invocations",
    "skill_usage",
    "subagent_steps",
    "subagents",
    "images",
    "loss_reports",
    "session_events",
    "message_embeddings",
    "user_steps",
    "human_turns",
    "session_outcomes",
)

# Hand-maintained column schema for every view in :data:`VIEW_NAMES`.
# Column ORDER matters — the drift test asserts tuple equality with
# ``DESCRIBE`` output, so a contributor who edits view DDL without updating
# this dict gets a hard CI failure rather than a runtime mystery.
VIEW_SCHEMA: dict[str, tuple[tuple[str, str], ...]] = {
    "sessions": (
        ("session_id", "VARCHAR"),
        ("agent", "VARCHAR"),
        ("agent_version", "VARCHAR"),
        ("cwd", "VARCHAR"),
        ("git_branch", "VARCHAR"),
        ("started_at", "TIMESTAMP"),
        ("ended_at", "TIMESTAMP"),
        ("agent_steps", "BIGINT"),
        ("step_count", "BIGINT"),
        ("model_name", "VARCHAR"),
        ("total_cost_usd", "DOUBLE"),
        ("reported_cost_usd", "DOUBLE"),
        ("trajectory_path", "VARCHAR"),
    ),
    "steps": (
        ("session_id", "VARCHAR"),
        ("step_id", "BIGINT"),
        ("ts", "TIMESTAMP"),
        ("source", "VARCHAR"),
        ("model_name", "VARCHAR"),
        ("message", "VARCHAR"),
        ("is_sidechain", "BOOLEAN"),
        ("is_compact_summary", "BOOLEAN"),
        ("prompt_tokens", "BIGINT"),
        ("completion_tokens", "BIGINT"),
        ("cached_tokens", "BIGINT"),
        ("cache_creation", "BIGINT"),
        ("llm_call_count", "BIGINT"),
        ("source_uuids", "JSON"),
        ("agent_id", "VARCHAR"),
        ("images", "JSON"),
    ),
    "messages": (
        ("uuid", "VARCHAR"),
        ("parent_uuid", "VARCHAR"),
        ("session_id", "VARCHAR"),
        ("ts", "TIMESTAMP"),
        ("type", "VARCHAR"),
        ("is_sidechain", "BOOLEAN"),
        ("is_compact_summary", "BOOLEAN"),
        ("message_id", "VARCHAR"),
        ("source_file", "VARCHAR"),
    ),
    "tool_calls": (
        ("session_id", "VARCHAR"),
        ("step_id", "BIGINT"),
        ("ts", "TIMESTAMP"),
        ("tool_name", "VARCHAR"),
        ("tool_use_id", "VARCHAR"),
        ("tool_input", "JSON"),
    ),
    "tool_results": (
        ("session_id", "VARCHAR"),
        ("step_id", "BIGINT"),
        ("ts", "TIMESTAMP"),
        ("tool_use_id", "VARCHAR"),
        ("content", "JSON"),
        ("is_error", "BOOLEAN"),
        ("exit_code", "BIGINT"),
        ("interrupted", "BOOLEAN"),
        ("images", "JSON"),
    ),
    "todo_events": (
        ("session_id", "VARCHAR"),
        ("written_at", "TIMESTAMP"),
        ("step_id", "BIGINT"),
        ("subject", "VARCHAR"),
        ("status", "VARCHAR"),
        ("active_form", "VARCHAR"),
        ("snapshot_ix", "BIGINT"),
    ),
    "todo_state_current": (
        ("session_id", "VARCHAR"),
        ("subject", "VARCHAR"),
        ("status", "VARCHAR"),
        ("active_form", "VARCHAR"),
        ("written_at", "TIMESTAMP"),
    ),
    "subagent_spawns": (
        ("session_id", "VARCHAR"),
        ("spawned_at", "TIMESTAMP"),
        ("step_id", "BIGINT"),
        ("tool_use_id", "VARCHAR"),
        ("spawn_tool", "VARCHAR"),
        ("subagent_type", "VARCHAR"),
        ("description", "VARCHAR"),
        ("prompt", "VARCHAR"),
        ("run_in_background", "VARCHAR"),
    ),
    "task_creations": (
        ("session_id", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
        ("step_id", "BIGINT"),
        ("tool_use_id", "VARCHAR"),
        ("create_tool", "VARCHAR"),
        ("subject", "VARCHAR"),
        ("description", "VARCHAR"),
        ("active_form", "VARCHAR"),
        ("metadata", "JSON"),
    ),
    "task_updates": (
        ("session_id", "VARCHAR"),
        ("updated_at", "TIMESTAMP"),
        ("step_id", "BIGINT"),
        ("tool_use_id", "VARCHAR"),
        ("update_tool", "VARCHAR"),
        ("task_id", "VARCHAR"),
        ("status", "VARCHAR"),
        ("add_blocked_by", "JSON"),
        ("owner", "VARCHAR"),
    ),
    "tasks_state_current": (
        ("session_id", "VARCHAR"),
        ("task_id", "VARCHAR"),
        ("subject", "VARCHAR"),
        ("active_form", "VARCHAR"),
        ("status", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
        ("last_updated_at", "TIMESTAMP"),
    ),
    "skill_invocations": (
        ("session_id", "VARCHAR"),
        ("ts", "TIMESTAMP"),
        ("step_id", "BIGINT"),
        ("source", "VARCHAR"),
        ("skill_id", "VARCHAR"),
        ("args", "VARCHAR"),
        ("tool_use_id", "VARCHAR"),
    ),
    "skill_usage": (
        ("session_id", "VARCHAR"),
        ("ts", "TIMESTAMP"),
        ("step_id", "BIGINT"),
        ("source", "VARCHAR"),
        ("skill_id", "VARCHAR"),
        ("args", "VARCHAR"),
        ("tool_use_id", "VARCHAR"),
        ("skill_name", "VARCHAR"),
        ("plugin", "VARCHAR"),
        ("is_builtin", "BOOLEAN"),
    ),
    "subagent_steps": (
        ("session_id", "VARCHAR"),
        ("step_id", "BIGINT"),
        ("ts", "TIMESTAMP"),
        ("source", "VARCHAR"),
        ("model_name", "VARCHAR"),
        ("message", "VARCHAR"),
        ("is_sidechain", "BOOLEAN"),
        ("is_compact_summary", "BOOLEAN"),
        ("prompt_tokens", "BIGINT"),
        ("completion_tokens", "BIGINT"),
        ("cached_tokens", "BIGINT"),
        ("cache_creation", "BIGINT"),
        ("llm_call_count", "BIGINT"),
        ("source_uuids", "JSON"),
        ("agent_id", "VARCHAR"),
        ("images", "JSON"),
    ),
    "subagents": (
        ("session_id", "VARCHAR"),
        ("agent_id", "VARCHAR"),
        ("agent_type", "VARCHAR"),
        ("description", "VARCHAR"),
        ("parent_tool_call_id", "VARCHAR"),
        ("parent_step_id", "BIGINT"),
        ("link_source", "VARCHAR"),
        ("spawn_depth", "BIGINT"),
        ("parent_agent_id", "VARCHAR"),
        ("first_ts", "TIMESTAMP"),
        ("last_ts", "TIMESTAMP"),
        ("step_count", "BIGINT"),
    ),
    "images": (
        ("session_id", "VARCHAR"),
        ("step_id", "BIGINT"),
        ("ts", "TIMESTAMP"),
        ("origin", "VARCHAR"),
        ("tool_use_id", "VARCHAR"),
        ("sha256", "VARCHAR"),
        ("media_type", "VARCHAR"),
        ("size_bytes", "BIGINT"),
        ("width", "BIGINT"),
        ("height", "BIGINT"),
        ("blob_path", "VARCHAR"),
    ),
    "loss_reports": (
        ("session_id", "VARCHAR"),
        ("records_total", "BIGINT"),
        ("records_converted", "BIGINT"),
        ("records_captured", "BIGINT"),
        ("records_dropped", "BIGINT"),
        ("gaps_observed", "JSON"),
        ("record_counts", "JSON"),
        ("subagent_files_found", "BIGINT"),
        ("subagent_files_convertible", "BIGINT"),
        ("workflow_subagent_files_found", "BIGINT"),
        ("report_path", "VARCHAR"),
    ),
    "session_events": (
        ("session_id", "VARCHAR"),
        ("seq", "BIGINT"),
        ("ts", "TIMESTAMP"),
        ("event_type", "VARCHAR"),
        ("subtype", "VARCHAR"),
        ("uuid", "VARCHAR"),
        ("parent_uuid", "VARCHAR"),
        ("tool_use_id", "VARCHAR"),
        ("is_sidechain", "BOOLEAN"),
        ("source_file", "VARCHAR"),
        ("payload", "JSON"),
        ("payload_bytes", "BIGINT"),
        ("payload_truncated", "BOOLEAN"),
    ),
    # VSS surface bound by ``register_vss``: a view over the Lance-attached
    # embeddings table, or an empty fallback table with the same shape when
    # no store exists yet. ``embedding`` is FLOAT[dim] — the catalog pins
    # the 1024 default; a store stamped at another Matryoshka width binds at
    # that width instead (the drift test runs against the default).
    "message_embeddings": (
        ("uuid", "VARCHAR"),
        ("model", "VARCHAR"),
        ("dim", "INTEGER"),
        ("embedding", "FLOAT[1024]"),
        ("embedded_at", "TIMESTAMP WITH TIME ZONE"),
    ),
    # Authorship surface bound by ``register_authorship`` over ``steps``: no
    # analytics run needed. ``author`` values and rules live in
    # :mod:`atif_duck.domain.authorship`.
    "user_steps": (
        ("session_id", "VARCHAR"),
        ("step_id", "BIGINT"),
        ("ts", "TIMESTAMP"),
        ("uuid", "VARCHAR"),
        ("is_sidechain", "BOOLEAN"),
        ("is_compact_summary", "BOOLEAN"),
        ("author", "VARCHAR"),
        ("message", "VARCHAR"),
    ),
    "human_turns": (
        ("session_id", "VARCHAR"),
        ("step_id", "BIGINT"),
        ("ts", "TIMESTAMP"),
        ("uuid", "VARCHAR"),
        ("message", "VARCHAR"),
    ),
    "session_outcomes": (
        ("session_id", "VARCHAR"),
        ("kind", "VARCHAR"),
        ("outcome", "VARCHAR"),
        ("human_turns", "BIGINT"),
        ("interrupts", "BIGINT"),
        ("reviewer_blocks", "BIGINT"),
    ),
}

# The core macros: the ones ``register_macros`` creates over the
# always-present transcript-derived views, ``semantic_search``, which needs the
# embedding store and is skipped when ``skip_vss=True``, and ``step_author``,
# which ``register_authorship`` creates beside its views. The analytics macros
# are NOT here — they live in :data:`ANALYTICS_MACRO_SIGNATURES` and register
# separately, because their backing views exist only once the pipelines run.
MACRO_NAMES: tuple[str, ...] = (
    "ago",
    "model_used",
    "cost_estimate",
    "tool_rank",
    "todo_velocity",
    "subagent_fanout",
    "semantic_search",
    "skill_rank",
    "skill_source_mix",
    "step_author",
)

# Hand-maintained signatures for every macro in :data:`MACRO_NAMES`. They are
# hand-maintained because DuckDB's ``duckdb_functions()`` returns NULL
# ``parameters`` for a table macro, so runtime introspection cannot recover
# them; the regex drift test over the registry's DDL text guards this dict
# instead. Parameter NAMES are part of the surface: they are what
# ``atif-sql schema`` prints and what ARG_EXEMPLARS keys the examples on.
MACRO_SIGNATURES: dict[str, tuple[str, ...]] = {
    "ago": ("interval_text",),
    "model_used": ("sid",),
    "cost_estimate": ("sid",),
    "tool_rank": ("last_n_days",),
    "todo_velocity": ("sid",),
    "subagent_fanout": ("sid",),
    "semantic_search": ("query_vec", "k"),
    "skill_rank": ("last_n_days",),
    "skill_source_mix": ("last_n_days",),
    "step_author": ("src", "msg"),
}

# ---------------------------------------------------------------------------
# v2 analytics surface (views + macros over the atif-analytics parquets).
# These bind ONLY when the backing parquet exists (a fresh corpus has none),
# so they live in their own catalogs rather than VIEW_NAMES/VIEW_SCHEMA —
# the DESCRIBE drift test binds them over a fixture with parquets present.
# ---------------------------------------------------------------------------

# Views registered by ``atif_duck.infrastructure.analytics.register_analytics``
# (parquet-gated; ``session_goals`` / ``conflicts_summary`` /
# ``perceived_summary`` derive from their upstream view).
#
# Removed 2026-09-27 with the trajectory and structural pipelines:
# ``message_trajectory``, ``message_clusters``, ``cluster_terms``,
# ``session_communities``, ``community_profile``.
ANALYTICS_VIEW_NAMES: tuple[str, ...] = (
    "session_classifications",
    "session_goals",
    "session_conflicts",
    "conflicts_summary",
    "user_friction",
    "perceived_errors",
    "perceived_summary",
)

# Column schema for every analytics view, in DESCRIBE order. Drift-tested by
# ``test_analytics_view_schema_matches_describe`` over a fixture corpus with
# every parquet present, and printed by ``atif-sql schema`` under
# ``requires: analytics``.
ANALYTICS_VIEW_SCHEMA: dict[str, tuple[tuple[str, str], ...]] = {
    "session_classifications": (
        ("session_id", "VARCHAR"),
        ("work_category", "VARCHAR"),
        ("goal", "VARCHAR"),
        ("confidence", "FLOAT"),
        ("classified_at", "TIMESTAMP WITH TIME ZONE"),
        ("autonomy_tier", "VARCHAR"),
        ("success", "VARCHAR"),
        ("category", "VARCHAR"),
    ),
    "session_goals": (
        ("session_id", "VARCHAR"),
        ("goal", "VARCHAR"),
        ("confidence", "FLOAT"),
        ("classified_at", "TIMESTAMP WITH TIME ZONE"),
    ),
    "session_conflicts": (
        ("session_id", "VARCHAR"),
        ("turn_a_uuid", "VARCHAR"),
        ("turn_b_uuid", "VARCHAR"),
        ("conflict_kind", "VARCHAR"),
        ("severity", "VARCHAR"),
        ("agent_position", "VARCHAR"),
        ("user_position", "VARCHAR"),
        ("confidence", "DOUBLE"),
        ("detected_at", "TIMESTAMP WITH TIME ZONE"),
    ),
    "conflicts_summary": (
        ("session_id", "VARCHAR"),
        ("conflict_count", "BIGINT"),
    ),
    "user_friction": (
        ("uuid", "VARCHAR"),
        ("session_id", "VARCHAR"),
        ("ts", "TIMESTAMP WITH TIME ZONE"),
        ("text_snippet", "VARCHAR"),
        ("label", "VARCHAR"),
        ("rationale", "VARCHAR"),
        ("source", "VARCHAR"),
        ("confidence", "FLOAT"),
        ("classified_at", "TIMESTAMP WITH TIME ZONE"),
    ),
    "perceived_errors": (
        ("session_id", "VARCHAR"),
        ("turn_uuid", "VARCHAR"),
        ("signal", "VARCHAR"),
        ("severity", "VARCHAR"),
        ("evidence", "VARCHAR"),
        ("agent_error_summary", "VARCHAR"),
        ("confidence", "DOUBLE"),
        ("detected_at", "TIMESTAMP WITH TIME ZONE"),
    ),
    "perceived_summary": (
        ("session_id", "VARCHAR"),
        ("n_errors", "BIGINT"),
        ("max_severity", "VARCHAR"),
        ("signals", "VARCHAR[]"),
    ),
}

# The macros ``register_analytics_macros`` creates. Each one registers only
# when every view it binds against exists, so a corpus with no analytics run
# has these names in the catalog and not on the connection — which is what
# ``requires: analytics`` on an example means. The regex drift test parses the
# signatures out of the DDL source.
#
# Removed 2026-09-27: ``autonomy_trend``, ``success_rate_by_work``,
# ``sentiment_arc``, ``cluster_top_terms``, ``community_top_topics``.
ANALYTICS_MACRO_SIGNATURES: dict[str, tuple[str, ...]] = {
    "work_mix": ("since_days",),
    "friction_counts": ("since_days",),
    "friction_rate": ("since_days",),
    "friction_examples": ("label_name", "n"),
    "conflicts_over_time": ("since_days",),
    "perceived_counts": ("since_days",),
    "perceived_rate": ("since_days",),
    "perceived_examples": ("signal_name", "n"),
}

# Macros whose DDL is ``CREATE OR REPLACE MACRO ... AS TABLE`` — callers
# write ``SELECT * FROM name(args)``; every other macro is scalar and is
# called as ``SELECT name(args)``. The examples generator derives its SQL
# shape from this set; drift against the actual DDL is caught by the
# ``AS TABLE`` regex test in ``tests/test_examples.py``.
TABLE_MACRO_NAMES: frozenset[str] = frozenset(
    {
        "cost_estimate",
        "tool_rank",
        "semantic_search",
        "skill_rank",
        "skill_source_mix",
        *ANALYTICS_MACRO_SIGNATURES,
    }
)

# ---------------------------------------------------------------------------
# Agent-facing descriptions — the ONLY hand-maintained text on the examples
# surface. One entry per view and macro across every catalog above; the
# coverage drift test in ``tests/test_examples.py`` fails when a catalog
# object is added without a description (or a description goes stale-keyed).
# ---------------------------------------------------------------------------

DESCRIPTIONS: dict[str, str] = {
    # -- core views ---------------------------------------------------------
    "sessions": (
        "One row per materialized session: which agent wrote it, timing, step "
        "counts, model, cost. total_cost_usd is our estimate from token counts "
        "(NULL when any step's model has no price); reported_cost_usd is what "
        "Claude Code itself recorded (its last cost-state record), NULL for Codex "
        "and for sessions without one."
    ),
    "steps": "One row per ATIF step (turn): flattened message text plus token metrics.",
    "messages": "Raw-record identity from edges.jsonl: uuid, parent_uuid, type, timestamp.",
    "tool_calls": "One row per tool call: tool_name plus JSON tool_input.",
    "tool_results": (
        "One row per tool result; join tool_calls USING (tool_use_id). is_error is "
        "the flag the agent recorded, exit_code the process exit code where the "
        "transcript states one (Claude Code states it only for a failed Bash call), "
        "interrupted is Bash's interrupted flag; NULL means not stated. Inline images "
        "are replaced in content by [image sha256:<hash> <media> <n> bytes] and "
        "listed in images."
    ),
    "todo_events": "Every TodoWrite snapshot item with status and snapshot index.",
    "todo_state_current": "Latest todo status per (session_id, subject).",
    "subagent_spawns": "Task/Agent launches: subagent_type, description, prompt.",
    "task_creations": "TaskCreate calls: subject, description, metadata.",
    "task_updates": "TaskUpdate calls: task_id, status, owner.",
    "tasks_state_current": "Latest status per persistent task, recovered from tool results.",
    "skill_invocations": "Skill tool calls and /slash-command invocations, unioned.",
    "skill_usage": "skill_invocations plus derived skill_name, plugin, is_builtin.",
    "subagent_steps": "Only the inlined subagent (sidechain) steps; agent_id says whose.",
    "subagents": (
        "One row per subagent a session spawned: its type, description, and the "
        "Task/Agent call that spawned it (parent_tool_call_id joins tool_calls."
        "tool_use_id; link_source says whether the agent-*.meta.json sidecar or the "
        "call's own result supplied it), plus the span and count of its steps."
    ),
    "images": (
        "One row per image lifted out of a tool result or a user message into the "
        "corpus blob store: sha256, media type, size, pixel dimensions, and "
        "blob_path relative to the corpus root."
    ),
    "loss_reports": (
        "Per-session conversion loss accounting from atif-converter: records "
        "converted to steps, captured as session_events rows, or dropped."
    ),
    "session_events": (
        "Non-message transcript records, one row each: hooks (event_type "
        "'attachment', subtype 'hook_success' / 'hook_blocking_error' / ...; "
        "'system' / 'stop_hook_summary'), injected context ('queued_command', "
        "'hook_additional_context'), 'api_error', 'compact_boundary', "
        "'model_refusal_fallback', 'cost-state', 'mode', 'permission-mode'; for "
        "Codex 'compacted' and event_msg 'turn_aborted' / 'error'. payload is the "
        "record body, cut to 8 KB (payload_bytes = original size, "
        "payload_truncated says whether it was cut). ts is NULL for cost-state and "
        "mode rows; order by seq. parent_uuid joins steps.source_uuids."
    ),
    "message_embeddings": "Step embeddings from the Lance store: uuid, model, dim, vector.",
    "user_steps": (
        "Every user-role step with its author: human, stop_hook, task_notification, "
        "harness, or audit_prompt. Machine-written text arrives in the user role too."
    ),
    "human_turns": "Main-chain user steps a human wrote; the rate macros count these.",
    "session_outcomes": (
        "Deterministic per-session outcome: kind (interactive | one_shot_job | "
        "turn_audit), outcome (pass | block | reviewer_blocked | interrupted | "
        "clean_end), and the human_turns / interrupts / reviewer_blocks counts behind them."
    ),
    # -- core macros --------------------------------------------------------
    "ago": "Timestamp N ago; use in filters like WHERE ts >= ago('7 days').",
    "model_used": "Model name one session used.",
    "cost_estimate": (
        "Estimated USD cost of one session from token counts and pricing, as "
        "(est_cost_usd, priced_steps, unpriced_steps). est_cost_usd covers the "
        "PRICED steps only — trust it only when unpriced_steps = 0. Both counts "
        "cover agent steps; user turns carry no model and are excluded. Cache "
        "reads are charged at zero, so the estimate is a lower bound."
    ),
    "tool_rank": "Tool call counts over the last N days, most used first.",
    "todo_velocity": "Completed-to-distinct-subject todo ratio for one session.",
    "subagent_fanout": "Number of Task/Agent subagent launches in one session.",
    "semantic_search": (
        "Top-k nearest steps by cosine over message_embeddings; sim and distance are "
        "the two halves of one metric (distance = 1 - sim). Two-step for text queries: "
        "embed the query text first (atif-sql search does both steps), then pass the "
        "dim-wide vector. Probes must be UNIT-NORM — stored vectors are un-normalized, "
        "so a raw-magnitude probe ranks by length instead of meaning."
    ),
    "skill_rank": "Skill/slash-command leaderboard over the last N days.",
    "skill_source_mix": "Per skill: tool vs slash-command invocation counts (non-builtin).",
    "step_author": (
        "Who wrote a step: NULL for non-user sources, else human, stop_hook, "
        "task_notification, harness, or audit_prompt (prefix rules)."
    ),
    # -- analytics views ----------------------------------------------------
    "session_classifications": (
        "LLM session labels: work category and goal. autonomy_tier and success are "
        "always-NULL compatibility columns; use session_outcomes instead."
    ),
    "session_goals": "Session goal text plus confidence (session_classifications projection).",
    "session_conflicts": "LLM-detected agent/user conflicts per session.",
    "conflicts_summary": "Conflict counts per session.",
    "user_friction": "Labeled user friction moments (correction, confusion, ...).",
    # -- analytics macros ---------------------------------------------------
    "work_mix": "Work-category counts over the last N days.",
    "friction_counts": "Counts per friction label over the last N days (NULL = full corpus).",
    "friction_rate": "Per-session friction hits vs human turn count.",
    "friction_examples": "Top-N example user messages for one friction label.",
    "perceived_errors": "User-perceived agent errors (7 signals), uuid-anchored with evidence.",
    "perceived_summary": "Per-session perceived-error rollup: count, max severity, signals.",
    "perceived_counts": "Counts per perceived-error signal over the last N days.",
    "perceived_rate": "Per-session perceived-error pressure vs human turn count.",
    "perceived_examples": "Top-N evidence quotes for one perceived-error signal.",
    "conflicts_over_time": "Conflicts on the conversation-time axis over the last N days.",
}

# Model pricing per 1M tokens (in_rate, out_rate) at public list rates from
# Anthropic's published pricing page. The ``cost_estimate`` macro LEFT JOINs
# step models against this table with a dated-suffix-stripping prefix match; a
# model absent here is counted in that macro's ``unpriced_steps`` rather than
# silently dropped, so omitting an unpriceable model understates nothing — it
# just declares itself.
#
# Base input/output rates only: prompt-cache write and read multipliers (1.25x
# / 2x / 0.1x of base input) are NOT modeled, matching the macro's formula,
# which charges uncached input at the base rate and leaves cache reads free.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
