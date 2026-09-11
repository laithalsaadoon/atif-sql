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
    "loss_reports",
    "message_embeddings",
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
    ),
    "loss_reports": (
        ("session_id", "VARCHAR"),
        ("records_total", "BIGINT"),
        ("records_converted", "BIGINT"),
        ("records_dropped", "BIGINT"),
        ("gaps_observed", "JSON"),
        ("record_counts", "JSON"),
        ("subagent_files_found", "BIGINT"),
        ("subagent_files_convertible", "BIGINT"),
        ("workflow_subagent_files_found", "BIGINT"),
        ("report_path", "VARCHAR"),
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
}

# The nine macros ``register_macros`` creates: eight over the always-present
# transcript-derived views, plus ``semantic_search``, which needs the
# embedding store and is skipped when ``skip_vss=True``. The analytics macros
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
}

# ---------------------------------------------------------------------------
# v2 analytics surface (views + macros over the atif-analytics parquets).
# These bind ONLY when the backing parquet exists (a fresh corpus has none),
# so they live in their own catalogs rather than VIEW_NAMES/VIEW_SCHEMA —
# the DESCRIBE drift test binds them over a fixture with parquets present.
# ---------------------------------------------------------------------------

# Views registered by ``atif_duck.infrastructure.analytics.register_analytics``
# (parquet-gated; ``session_goals`` / ``conflicts_summary`` derive from their
# upstream view).
ANALYTICS_VIEW_NAMES: tuple[str, ...] = (
    "session_classifications",
    "session_goals",
    "message_trajectory",
    "session_conflicts",
    "conflicts_summary",
    "user_friction",
    "perceived_errors",
    "perceived_summary",
    "message_clusters",
    "cluster_terms",
    "session_communities",
    "community_profile",
)

# The 13 macros ``register_analytics_macros`` creates. Each one registers only
# when every view it binds against exists, so a corpus with no analytics run
# has these names in the catalog and not on the connection — which is what
# ``requires: analytics`` on an example means. The regex drift test parses the
# signatures out of the DDL source.
ANALYTICS_MACRO_SIGNATURES: dict[str, tuple[str, ...]] = {
    "autonomy_trend": ("window_days",),
    "work_mix": ("since_days",),
    "success_rate_by_work": ("since_days",),
    "sentiment_arc": ("sid",),
    "friction_counts": ("since_days",),
    "friction_rate": ("since_days",),
    "friction_examples": ("label_name", "n"),
    "conflicts_over_time": ("since_days",),
    "perceived_counts": ("since_days",),
    "perceived_rate": ("since_days",),
    "perceived_examples": ("signal_name", "n"),
    "cluster_top_terms": ("cid", "n"),
    "community_top_topics": ("cid", "n"),
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
        "counts, model, total cost."
    ),
    "steps": "One row per ATIF step (turn): flattened message text plus token metrics.",
    "messages": "Raw-record identity from edges.jsonl: uuid, parent_uuid, type, timestamp.",
    "tool_calls": "One row per tool call: tool_name plus JSON tool_input.",
    "tool_results": "One row per tool result; join tool_calls USING (tool_use_id).",
    "todo_events": "Every TodoWrite snapshot item with status and snapshot index.",
    "todo_state_current": "Latest todo status per (session_id, subject).",
    "subagent_spawns": "Task/Agent launches: subagent_type, description, prompt.",
    "task_creations": "TaskCreate calls: subject, description, metadata.",
    "task_updates": "TaskUpdate calls: task_id, status, owner.",
    "tasks_state_current": "Latest status per persistent task, recovered from tool results.",
    "skill_invocations": "Skill tool calls and /slash-command invocations, unioned.",
    "skill_usage": "skill_invocations plus derived skill_name, plugin, is_builtin.",
    "subagent_steps": "Only the inlined subagent (sidechain) steps.",
    "loss_reports": "Per-session conversion loss accounting from atif-converter.",
    "message_embeddings": "Step embeddings from the Lance store: uuid, model, dim, vector.",
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
    # -- analytics views ----------------------------------------------------
    "session_classifications": ("LLM session labels: autonomy tier, work category, success, goal."),
    "session_goals": "Session goal text plus confidence (session_classifications projection).",
    "message_trajectory": "Per-turn sentiment trajectory with transition labels.",
    "session_conflicts": "LLM-detected agent/user conflicts per session.",
    "conflicts_summary": "Conflict counts per session.",
    "user_friction": "Labeled user friction moments (correction, confusion, ...).",
    "message_clusters": "Cluster id per message uuid (HDBSCAN; -1 = noise).",
    "cluster_terms": "Top TF-IDF terms per cluster.",
    "session_communities": "Community id per session from the session graph.",
    "community_profile": "Community-detection quality profile (gamma sweep).",
    # -- analytics macros ---------------------------------------------------
    "autonomy_trend": "Weekly autonomy-tier mix over the last N days.",
    "work_mix": "Work-category counts over the last N days.",
    "success_rate_by_work": "Success/failure/partial rates per work category (known outcomes).",
    "sentiment_arc": "Chronological sentiment trajectory for one session.",
    "friction_counts": "Counts per friction label over the last N days (NULL = full corpus).",
    "friction_rate": "Per-session friction hits vs user message count.",
    "friction_examples": "Top-N example user messages for one friction label.",
    "perceived_errors": "User-perceived agent errors (7 signals), uuid-anchored with evidence.",
    "perceived_summary": "Per-session perceived-error rollup: count, max severity, signals.",
    "perceived_counts": "Counts per perceived-error signal over the last N days.",
    "perceived_rate": "Per-session perceived-error pressure vs user message count.",
    "perceived_examples": "Top-N evidence quotes for one perceived-error signal.",
    "conflicts_over_time": "Conflicts on the conversation-time axis over the last N days.",
    "cluster_top_terms": "Top-N TF-IDF terms for one cluster id.",
    "community_top_topics": "Top clusters within one community, each with its top terms.",
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
