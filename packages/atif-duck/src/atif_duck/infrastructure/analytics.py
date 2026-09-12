# SPDX-License-Identifier: Apache-2.0

"""v2 analytics views + macros over the atif-analytics parquet outputs.

atif-analytics writes its artifacts under ``<corpus_root>/analytics/``
(sharded dirs for the five LLM pipelines, single files for the structural
three); this module binds them as the 12 analytics views and 13 analytics
macros the catalog declares.

atif-duck may not import atif-analytics (independence contract), so the
artifact names are pinned here against the documented layout — the shape is
contract, not code sharing.

Gating: each view is created only when its backing parquet is populated on
disk; each macro is registered only when EVERY view it binds against was
registered. Missing analytics parquets are the default state until the
pipelines run, so skips log at DEBUG.

Column semantics worth stating once (repeated per macro below):

* ``sentiment_arc`` / ``conflicts_over_time`` join ``messages`` (the
  edges-derived uuid-keyed view); the role column is ``m.type`` (the raw
  record role) aliased to ``role``.
* ``conflicts_over_time``'s ``root_session_id`` equals ``session_id``: harbor
  INLINES subagent transcripts (fidelity gap 4), so one conversation is one
  session and there is no parent session to collapse onto.
* ``friction_rate``'s user-message denominator counts user-role main-chain
  text steps from ``steps``, so it is a STEP count, not a raw message count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

# ---------------------------------------------------------------------------
# Artifact layout (pinned against atif-analytics' AnalyticsLayout — contract)
# ---------------------------------------------------------------------------

#: Prefix of the per-view parquet reader each analytics view selects from.
#: ``CREATE VIEW`` cannot take a bound parameter, so the parquet paths go
#: through the connection's ``read_parquet`` relation API (a Python value, not
#: statement text), the relation is registered under ``<prefix><view_name>``,
#: and the view itself interpolates constants only.
_ANALYTICS_READER_PREFIX: str = "v_raw_analytics_"

#: view name -> path relative to <corpus_root>/analytics/. Directory entries
#: are sharded caches (part-*.parquet); file entries are single parquets.
_ANALYTICS_SOURCES: dict[str, str] = {
    "session_classifications": "session_classifications",
    "message_trajectory": "message_trajectory",
    "session_conflicts": "session_conflicts",
    "user_friction": "user_friction",
    "perceived_errors": "perceived_errors",
    "message_clusters": "clusters.parquet",
    "cluster_terms": "cluster_terms.parquet",
    "session_communities": "session_communities.parquet",
    "community_profile": "community_profile.parquet",
}

#: Convenience alias projections. Every entry is ADDITIVE (``*`` first), so a
#: query written against the parquet's own column names keeps working and the
#: alias is a second name for the same column, never a replacement.
_VIEW_PROJECTIONS: dict[str, str] = {
    "session_classifications": (
        "*, autonomy_tier AS autonomy, success AS success_outcome, work_category AS category"
    ),
    "message_trajectory": "*, curr_sentiment AS sentiment, is_transition AS transition",
}

#: Read-side dedup, belt-and-suspenders behind the writer's own idempotency:
#: if duplicate ``(session_id, prev_uuid, curr_uuid)`` rows ever land across
#: the trajectory shards, the latest ``classified_at`` wins at read time
#: rather than the view double-counting the pair.
_VIEW_QUALIFY: dict[str, str] = {
    "message_trajectory": (
        "row_number() OVER ("
        "PARTITION BY session_id, prev_uuid, curr_uuid "
        "ORDER BY classified_at DESC NULLS LAST"
        ") = 1"
    ),
}

#: Per-macro view dependencies, keyed on the views this module registers. A
#: macro whose entry is unsatisfied is skipped: DuckDB binds a macro body at
#: CREATE time, so registering it against an absent view fails outright.
_ANALYTICS_MACRO_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "autonomy_trend": ("session_classifications",),
    "work_mix": ("session_classifications",),
    "success_rate_by_work": ("session_classifications",),
    "sentiment_arc": ("message_trajectory",),
    "friction_counts": ("user_friction",),
    "friction_rate": ("user_friction",),
    "friction_examples": ("user_friction",),
    "conflicts_over_time": ("session_conflicts",),
    "perceived_counts": ("perceived_errors",),
    "perceived_rate": ("perceived_errors",),
    "perceived_examples": ("perceived_errors",),
    "cluster_top_terms": ("cluster_terms",),
    "community_top_topics": ("cluster_terms", "session_communities", "message_clusters"),
}


#: Smallest byte count a readable parquet file can have: the format frames every
#: file as ``PAR1`` + footer + 4-byte footer length + ``PAR1``. A shorter file is
#: what a crashed writer leaves behind, and DuckDB errors on it rather than
#: reading zero rows, so a view built over one fails to register. Declared here
#: rather than imported because atif-duck may never import atif-analytics.
_MIN_PARQUET_BYTES = 16


def _backing_files(path: Path) -> list[Path]:
    """The parquet files backing one artifact (sharded dir or single file)."""
    if not path.exists():
        return []
    if path.is_dir():
        return sorted(
            p for p in path.glob("part-*.parquet") if p.stat().st_size > _MIN_PARQUET_BYTES
        )
    return [path] if path.stat().st_size > _MIN_PARQUET_BYTES else []


def register_analytics(con: duckdb.DuckDBPyConnection, corpus_root: Path) -> set[str]:
    """Bind the analytics parquets as views; return the set registered.

    Each view is created only when its backing parquet is populated.
    Derived views (``session_goals``, ``conflicts_summary``) follow their
    upstream. Idempotent against a partially-populated corpus.
    """
    analytics_dir = corpus_root / "analytics"
    registered: set[str] = set()

    for view_name, rel in _ANALYTICS_SOURCES.items():
        parts = _backing_files(analytics_dir / rel)
        if not parts:
            logger.debug(
                "register_analytics: skipping {} (no parquet at {})",
                view_name,
                analytics_dir / rel,
            )
            continue
        projection = _VIEW_PROJECTIONS.get(view_name, "*")
        qualify = _VIEW_QUALIFY.get(view_name)
        qualify_clause = f" QUALIFY {qualify}" if qualify else ""
        reader = f"{_ANALYTICS_READER_PREFIX}{view_name}"
        try:
            # The parquet paths never enter SQL text: the relation API takes
            # them as a Python list, and the relation is registered as the
            # view's reader. It stays a lazy read_parquet scan (the optimizer
            # inlines the reader into the view), so the file is still opened
            # per query. Not ``con.sql("... read_parquet(?)", params=...)``:
            # that shape measured about 3x the memory of a plain view.
            con.read_parquet([str(p) for p in parts]).create_view(reader, replace=True)
            # View name, projection and qualify clause are module constants
            # (_ANALYTICS_SOURCES / _VIEW_PROJECTIONS / _VIEW_QUALIFY); the reader
            # name is a constant prefix plus that view name.
            con.execute(
                f"CREATE OR REPLACE VIEW {view_name} AS "  # noqa: S608  # nosec B608 - constants only; paths are bound into the reader relation
                f"SELECT {projection} FROM {reader}"
                f"{qualify_clause};"
            )
            logger.debug("Registered analytics view: {}", view_name)
            registered.add(view_name)
        except Exception:
            # register-or-fail-loud for real DDL errors — a populated parquet
            # that cannot bind is corruption, not a fresh-install state.
            logger.exception(
                "Failed to register analytics view {} from {}", view_name, analytics_dir / rel
            )
            raise

    if "session_classifications" in registered:
        con.execute(
            """
            CREATE OR REPLACE VIEW session_goals AS
            SELECT session_id, goal, confidence, classified_at
            FROM session_classifications;
            """
        )
        registered.add("session_goals")
        logger.debug("Registered analytics view: session_goals")

    if "perceived_errors" in registered:
        # Sessions with zero perceived-error rows simply do not appear here
        # (clean sessions write nothing — LangSmith parity); callers wanting
        # every session LEFT JOIN onto ``sessions`` and coalesce. max_severity
        # orders minor < moderate < major; signals is the DISTINCT ordered
        # list of evidence categories seen in the session.
        con.execute(
            """
            CREATE OR REPLACE VIEW perceived_summary AS
            SELECT session_id,
                   count(*) AS n_errors,
                   CASE max(CASE severity
                            WHEN 'major' THEN 3
                            WHEN 'moderate' THEN 2
                            ELSE 1 END)
                        WHEN 3 THEN 'major'
                        WHEN 2 THEN 'moderate'
                        ELSE 'minor' END AS max_severity,
                   list_sort(list(DISTINCT signal)) AS signals
            FROM perceived_errors
            GROUP BY session_id;
            """
        )
        registered.add("perceived_summary")
        logger.debug("Registered analytics view: perceived_summary")

    if "session_conflicts" in registered:
        # A session with zero conflict rows is ABSENT here, not present with
        # conflict_count = 0. A caller that wants every session must LEFT JOIN
        # onto ``sessions`` and coalesce, or its denominator silently drops the
        # conflict-free sessions — which are the majority.
        con.execute(
            """
            CREATE OR REPLACE VIEW conflicts_summary AS
            SELECT session_id, count(*) AS conflict_count
            FROM session_conflicts
            GROUP BY session_id;
            """
        )
        registered.add("conflicts_summary")
        logger.debug("Registered analytics view: conflicts_summary")

    return registered


def register_analytics_macros(
    con: duckdb.DuckDBPyConnection,
    registered_views: set[str],
) -> None:
    """Create the 13 analytics macros whose backing views are registered.

    Every signature must match its ``ANALYTICS_MACRO_SIGNATURES`` entry in
    :mod:`atif_duck.domain.catalog`; the drift test parses this function's
    DDL text and fails on any divergence.

    Must run after :func:`register_analytics` AND the base
    ``register_views`` — several macros join the transcript-derived views
    (``sessions``, ``steps``, ``messages``).
    """
    analytics_macros: list[tuple[str, str]] = [
        # Time series: autonomy tier mix over rolling windows. Buckets by
        # ``sessions.started_at`` (conversation time), NOT ``classified_at``
        # (classifier run time). The two clocks are unrelated: a one-shot
        # backfill stamps every row with the same classified_at, which would
        # collapse the whole corpus into a single bucket.
        (
            "autonomy_trend",
            """
            CREATE OR REPLACE MACRO autonomy_trend(window_days) AS TABLE (
                SELECT
                    date_trunc('week', s.started_at) AS week,
                    sc.autonomy_tier,
                    count(*) AS n
                FROM session_classifications sc
                JOIN sessions s
                  ON s.session_id = sc.session_id
                WHERE s.started_at >= current_timestamp - (window_days * INTERVAL 1 DAY)
                GROUP BY 1, 2
                ORDER BY 1, 2
            );
            """,
        ),
        # Work-category mix in the last N days.
        (
            "work_mix",
            """
            CREATE OR REPLACE MACRO work_mix(since_days) AS TABLE (
                SELECT work_category, count(*) AS n
                FROM session_classifications
                WHERE classified_at >= current_timestamp - (since_days * INTERVAL 1 DAY)
                GROUP BY 1
                ORDER BY n DESC
            );
            """,
        ),
        # Success/failure/partial rates broken down by work category.
        # DENOMINATOR = KNOWN outcomes, not all sessions. ``unknown``
        # correlates with work category, so folding it into the denominator
        # would depress exactly the categories it concentrates in. The three
        # rates divide by ``known_sessions`` and ``unknown_fraction`` is its
        # own column, which a reader must check before trusting the rates.
        (
            "success_rate_by_work",
            """
            CREATE OR REPLACE MACRO success_rate_by_work(since_days) AS TABLE (
                SELECT
                    work_category,
                    count(*) AS sessions,
                    count(*) FILTER (WHERE success != 'unknown') AS known_sessions,
                    count(*) FILTER (WHERE success = 'unknown')::DOUBLE
                        / NULLIF(count(*), 0) AS unknown_fraction,
                    count(*) FILTER (WHERE success = 'success')::DOUBLE
                        / NULLIF(count(*) FILTER (WHERE success != 'unknown'), 0)
                        AS success_rate,
                    count(*) FILTER (WHERE success = 'failure')::DOUBLE
                        / NULLIF(count(*) FILTER (WHERE success != 'unknown'), 0)
                        AS failure_rate,
                    count(*) FILTER (WHERE success = 'partial')::DOUBLE
                        / NULLIF(count(*) FILTER (WHERE success != 'unknown'), 0)
                        AS partial_rate
                FROM session_classifications
                WHERE classified_at >= current_timestamp - (since_days * INTERVAL 1 DAY)
                GROUP BY 1
                ORDER BY sessions DESC
            );
            """,
        ),
        # Sentiment arc for one session, chronological. Joins the anchor
        # turn (curr_uuid) back to the edges-derived ``messages`` view for
        # the conversation timestamp; ``m.type`` carries the raw role.
        (
            "sentiment_arc",
            """
            CREATE OR REPLACE MACRO sentiment_arc(sid) AS TABLE (
                SELECT m.ts,
                       m.type AS role,
                       mt.curr_sentiment,
                       mt.delta,
                       mt.transition_kind,
                       mt.is_transition,
                       mt.confidence
                  FROM messages m
                  JOIN message_trajectory mt
                    ON m.uuid = mt.curr_uuid
                 WHERE m.session_id = sid
                 ORDER BY m.ts
            );
            """,
        ),
        # Counts per friction label over the last N days by message ``ts``
        # (the user's utterance time). NULL = full corpus. Excludes
        # label='none' (the majority sentinel would swamp the output).
        (
            "friction_counts",
            """
            CREATE OR REPLACE MACRO friction_counts(since_days) AS TABLE (
                SELECT label,
                       count(*)                       AS n,
                       count(DISTINCT session_id)     AS sessions,
                       avg(confidence)                AS avg_confidence,
                       sum(CASE WHEN source='regex' THEN 1 ELSE 0 END) AS n_regex,
                       sum(CASE WHEN source='llm'   THEN 1 ELSE 0 END) AS n_llm
                  FROM user_friction
                 WHERE label != 'none'
                   AND (since_days IS NULL
                        OR ts >= current_timestamp - (since_days * INTERVAL 1 DAY))
                 GROUP BY label
                 ORDER BY n DESC
            );
            """,
        ),
        # Per-session friction pressure vs the user message count. The
        # denominator counts user-role main-chain text STEPS from ``steps``,
        # so it is a step count and not a raw transcript message count.
        (
            "friction_rate",
            """
            CREATE OR REPLACE MACRO friction_rate(since_days) AS TABLE (
                WITH hits AS (
                    SELECT session_id,
                           count(*) FILTER (WHERE label != 'none') AS n_friction,
                           count(*) FILTER (WHERE label = 'status_ping')        AS n_status,
                           count(*) FILTER (WHERE label = 'unmet_expectation')  AS n_unmet,
                           count(*) FILTER (WHERE label = 'confusion')          AS n_confusion,
                           count(*) FILTER (WHERE label = 'interruption')       AS n_interruption,
                           count(*) FILTER (WHERE label = 'correction')         AS n_correction,
                           count(*) FILTER (WHERE label = 'frustration')        AS n_frustration
                      FROM user_friction
                     WHERE since_days IS NULL
                        OR ts >= current_timestamp - (since_days * INTERVAL 1 DAY)
                     GROUP BY session_id
                ),
                user_msgs AS (
                    SELECT s.session_id,
                           count(*) AS n_user_msgs
                      FROM steps s
                     WHERE s.source = 'user'
                       AND NOT s.is_sidechain
                       AND s.message IS NOT NULL
                       AND length(s.message) >= 1
                       AND (since_days IS NULL
                            OR s.ts >= current_timestamp - (since_days * INTERVAL 1 DAY))
                     GROUP BY 1
                )
                SELECT h.session_id,
                       h.n_friction,
                       h.n_status, h.n_unmet, h.n_confusion,
                       h.n_interruption, h.n_correction, h.n_frustration,
                       COALESCE(um.n_user_msgs, 0)                              AS n_user_msgs,
                       h.n_friction::DOUBLE / NULLIF(um.n_user_msgs, 0)         AS rate
                  FROM hits h
                  LEFT JOIN user_msgs um USING (session_id)
                 WHERE h.n_friction > 0
                 ORDER BY h.n_friction DESC
            );
            """,
        ),
        # Top-N example user messages for one friction label.
        (
            "friction_examples",
            """
            CREATE OR REPLACE MACRO friction_examples(label_name, n) AS TABLE (
                SELECT session_id, ts, text_snippet, rationale, source, confidence
                  FROM user_friction
                 WHERE label = label_name
                 ORDER BY confidence DESC, ts DESC
                 LIMIT n
            );
            """,
        ),
        # Conflicts on a conversation-time axis, NOT a detection-time one:
        # ``detected_at`` is the worker run-time clock, so the macro
        # recovers ``m.ts`` by joining the later turn of the pair
        # (``turn_b_uuid``) back to ``messages.uuid``. INNER JOIN on
        # purpose — the recoverable subset is the trustworthy subset.
        # root_session_id = session_id here: harbor inlines subagent
        # transcripts, so one conversation is one session.
        (
            "conflicts_over_time",
            """
            CREATE OR REPLACE MACRO conflicts_over_time(since_days) AS TABLE (
                SELECT m.ts                                          AS conversation_ts,
                       sc.session_id,
                       sc.session_id                                 AS root_session_id,
                       sc.turn_a_uuid,
                       sc.turn_b_uuid,
                       sc.conflict_kind,
                       sc.severity,
                       sc.agent_position,
                       sc.user_position,
                       sc.confidence,
                       sc.detected_at
                  FROM session_conflicts sc
                  JOIN messages m
                    ON m.uuid = sc.turn_b_uuid
                 WHERE since_days IS NULL
                    OR m.ts >= current_timestamp - (since_days * INTERVAL 1 DAY)
                 ORDER BY m.ts DESC
            );
            """,
        ),
        # Counts per perceived-error signal over the last N days on a REAL
        # conversation-time axis (conflicts_over_time precedent):
        # ``detected_at`` is the worker run-time clock, so the macro
        # recovers ``m.ts`` by joining the anchor USER turn (turn_uuid)
        # back to ``messages.uuid``. INNER JOIN on purpose — the
        # recoverable subset is the trustworthy subset. NULL = full corpus.
        (
            "perceived_counts",
            """
            CREATE OR REPLACE MACRO perceived_counts(since_days) AS TABLE (
                SELECT pe.signal,
                       count(*)                   AS n,
                       count(DISTINCT pe.session_id) AS sessions,
                       count(*) FILTER (WHERE pe.severity = 'major')    AS n_major,
                       count(*) FILTER (WHERE pe.severity = 'moderate') AS n_moderate,
                       count(*) FILTER (WHERE pe.severity = 'minor')    AS n_minor,
                       avg(pe.confidence)         AS avg_confidence
                  FROM perceived_errors pe
                  JOIN messages m
                    ON m.uuid = pe.turn_uuid
                 WHERE since_days IS NULL
                    OR m.ts >= current_timestamp - (since_days * INTERVAL 1 DAY)
                 GROUP BY pe.signal
                 ORDER BY n DESC
            );
            """,
        ),
        # Per-session perceived-error pressure vs the user message count
        # (friction_rate precedent: the denominator counts user-role
        # main-chain text steps). Sessions with zero perceived errors do
        # not appear — LEFT JOIN onto sessions and coalesce for the
        # full-population rate.
        (
            "perceived_rate",
            """
            CREATE OR REPLACE MACRO perceived_rate(since_days) AS TABLE (
                WITH hits AS (
                    SELECT pe.session_id,
                           count(*) AS n_errors,
                           count(*) FILTER (WHERE pe.severity = 'major')    AS n_major,
                           count(*) FILTER (WHERE pe.severity = 'moderate') AS n_moderate,
                           count(*) FILTER (WHERE pe.severity = 'minor')    AS n_minor
                      FROM perceived_errors pe
                      JOIN messages m
                        ON m.uuid = pe.turn_uuid
                     WHERE since_days IS NULL
                        OR m.ts >= current_timestamp - (since_days * INTERVAL 1 DAY)
                     GROUP BY pe.session_id
                ),
                user_msgs AS (
                    SELECT s.session_id,
                           count(*) AS n_user_msgs
                      FROM steps s
                     WHERE s.source = 'user'
                       AND NOT s.is_sidechain
                       AND s.message IS NOT NULL
                       AND length(s.message) >= 1
                       AND (since_days IS NULL
                            OR s.ts >= current_timestamp - (since_days * INTERVAL 1 DAY))
                     GROUP BY 1
                )
                SELECT h.session_id,
                       h.n_errors,
                       h.n_major, h.n_moderate, h.n_minor,
                       COALESCE(um.n_user_msgs, 0)                      AS n_user_msgs,
                       h.n_errors::DOUBLE / NULLIF(um.n_user_msgs, 0)   AS rate
                  FROM hits h
                  LEFT JOIN user_msgs um USING (session_id)
                 ORDER BY h.n_errors DESC
            );
            """,
        ),
        # Top-N examples for one perceived-error signal, highest-confidence
        # first (friction_examples precedent).
        (
            "perceived_examples",
            """
            CREATE OR REPLACE MACRO perceived_examples(signal_name, n) AS TABLE (
                SELECT session_id, turn_uuid, severity, evidence,
                       agent_error_summary, confidence, detected_at
                  FROM perceived_errors
                 WHERE signal = signal_name
                 ORDER BY confidence DESC, detected_at DESC
                 LIMIT n
            );
            """,
        ),
        # Top-N TF-IDF terms for a single cluster.
        (
            "cluster_top_terms",
            """
            CREATE OR REPLACE MACRO cluster_top_terms(cid, n) AS TABLE (
                SELECT term, weight, rank
                FROM cluster_terms
                WHERE cluster_id = cid
                ORDER BY rank
                LIMIT n
            );
            """,
        ),
        # Top cluster_ids within one community, ranked by contributed
        # messages, each with its top 5 TF-IDF terms.
        (
            "community_top_topics",
            """
            CREATE OR REPLACE MACRO community_top_topics(cid, n) AS TABLE (
                WITH community_msgs AS (
                    SELECT m.uuid AS uuid
                      FROM messages m
                      JOIN session_communities sc
                        ON m.session_id = sc.session_id
                     WHERE sc.community_id = cid
                ),
                cluster_counts AS (
                    SELECT mc.cluster_id, count(*) AS n_msgs
                      FROM message_clusters mc
                      JOIN community_msgs cm USING (uuid)
                     WHERE mc.cluster_id >= 0
                     GROUP BY mc.cluster_id
                )
                SELECT cc.cluster_id, cc.n_msgs,
                       (SELECT string_agg(term, ', ' ORDER BY rank)
                          FROM cluster_terms ct
                         WHERE ct.cluster_id = cc.cluster_id
                           AND ct.rank <= 5) AS top_terms
                  FROM cluster_counts cc
                  ORDER BY n_msgs DESC
                  LIMIT n
            );
            """,
        ),
    ]

    for macro_name, ddl in analytics_macros:
        required = _ANALYTICS_MACRO_REQUIREMENTS.get(macro_name, ())
        if any(view not in registered_views for view in required):
            logger.debug("Skipped analytics macro {} (view missing among {})", macro_name, required)
            continue
        try:
            con.execute(ddl)
            logger.debug("Registered analytics macro: {}", macro_name)
        except Exception:
            logger.exception("Failed to register analytics macro {}", macro_name)
            raise


__all__ = ["register_analytics", "register_analytics_macros"]
