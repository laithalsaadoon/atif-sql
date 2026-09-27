# SPDX-License-Identifier: Apache-2.0

"""The ``step_author`` macro and the three deterministic views built on it.

Every object here reads only the core ``steps`` view, so it registers on
every corpus, before any analytics pipeline has run (``requires: core``):

* ``step_author(src, msg)`` — scalar macro: who wrote a step
  (:mod:`atif_duck.domain.authorship` holds the rule table).
* ``user_steps`` — every user-role step with its ``author``.
* ``human_turns`` — main-chain user steps a human wrote. The analytics rate
  macros count their denominators from here.
* ``session_outcomes`` — one row per session: ``kind`` (interactive |
  one_shot_job | turn_audit), ``outcome`` (pass | block | reviewer_blocked |
  interrupted | clean_end) and the three counts behind them.

Must run after ``register_views`` (the bodies bind against ``steps`` at
CREATE time) and before the analytics macros, which read ``human_turns``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from atif_duck.domain.authorship import INTERRUPT_PREDICATE_SQL, STEP_AUTHOR_CASE_SQL

if TYPE_CHECKING:
    import duckdb

#: Leading ``{"ok": true`` / ``{"ok": false`` on an audit verdict, allowing a
#: code fence and any whitespace. Turn-audit sessions end on one of these.
_VERDICT_OK_TRUE_RE = r"""^\s*(```(json)?\s*)?\{\s*"ok"\s*:\s*true"""
_VERDICT_OK_FALSE_RE = r"""^\s*(```(json)?\s*)?\{\s*"ok"\s*:\s*false"""


def register_authorship(con: duckdb.DuckDBPyConnection) -> None:
    """Create ``step_author`` then ``user_steps``, ``human_turns``, ``session_outcomes``.

    Raises
    ------
    duckdb.Error
        If any DDL fails (register-or-fail-loud).
    """
    try:
        con.execute(f"CREATE OR REPLACE MACRO step_author(src, msg) AS ({STEP_AUTHOR_CASE_SQL});")

        # Every user-role step, sidechain included (flagged), with its author.
        # A compaction summary is harness whatever its text: the flag is the
        # converter's own record of what the step is.
        # ``uuid`` is the step's FIRST source uuid, the key user_friction.uuid
        # and perceived_errors.turn_uuid carry, so either joins here directly.
        con.execute(
            """
            CREATE OR REPLACE VIEW user_steps AS
            SELECT session_id,
                   step_id,
                   ts,
                   json_extract_string(source_uuids, '$[0]') AS uuid,
                   is_sidechain,
                   is_compact_summary,
                   CASE WHEN is_compact_summary THEN 'harness'
                        ELSE step_author(source, message) END  AS author,
                   message
              FROM steps
             WHERE source = 'user';
            """
        )

        # What the user actually said: main chain only (a sidechain's "user"
        # turn is the parent agent's prompt to a subagent).
        con.execute(
            """
            CREATE OR REPLACE VIEW human_turns AS
            SELECT session_id, step_id, ts, uuid, message
              FROM user_steps
             WHERE author = 'human'
               AND NOT is_sidechain;
            """
        )

        # One row per session with a main-chain step. ``kind`` comes from the
        # opening user step and the human-turn count; ``outcome`` is the FIRST
        # of these that holds: an audit verdict (pass / block) as the last
        # agent message, any Stop hook block, any interrupt, else clean_end.
        # The three counts are over the whole session, so ``reviewer_blocked``
        # means the reviewer blocked at least once, not that it ended blocked.
        con.execute(
            f"""
            CREATE OR REPLACE VIEW session_outcomes AS
            WITH authored AS (
                SELECT session_id, step_id, source, message,
                       CASE WHEN is_compact_summary AND source = 'user' THEN 'harness'
                            ELSE step_author(source, message) END AS author
                  FROM steps
                 WHERE NOT is_sidechain
            ),
            per_session AS (
                SELECT session_id,
                       arg_min(author, step_id) FILTER (WHERE source = 'user')
                           AS first_user_author,
                       arg_max(message, step_id)
                           FILTER (WHERE source = 'agent' AND length(message) > 0)
                           AS last_agent_message,
                       count(*) FILTER (WHERE author = 'human') AS human_turns,
                       count(*) FILTER (
                           WHERE author = 'harness' AND {INTERRUPT_PREDICATE_SQL}
                       ) AS interrupts,
                       count(*) FILTER (WHERE author = 'stop_hook') AS reviewer_blocks
                  FROM authored
                 GROUP BY session_id
            )
            SELECT session_id,
                   CASE
                       WHEN first_user_author = 'audit_prompt' THEN 'turn_audit'
                       WHEN human_turns <= 1 THEN 'one_shot_job'
                       ELSE 'interactive'
                   END AS kind,
                   CASE
                       WHEN regexp_matches(last_agent_message, '{_VERDICT_OK_TRUE_RE}') THEN 'pass'
                       WHEN regexp_matches(last_agent_message, '{_VERDICT_OK_FALSE_RE}') THEN 'block'
                       WHEN reviewer_blocks > 0 THEN 'reviewer_blocked'
                       WHEN interrupts > 0 THEN 'interrupted'
                       ELSE 'clean_end'
                   END AS outcome,
                   human_turns,
                   interrupts,
                   reviewer_blocks
              FROM per_session;
            """  # noqa: S608  # nosec B608 - module constants and domain SqlFragment constants only
        )
        logger.debug("Registered step_author, user_steps, human_turns, session_outcomes")
    except Exception:
        logger.exception("Failed to register the authorship surface")
        raise


__all__ = ["register_authorship"]
