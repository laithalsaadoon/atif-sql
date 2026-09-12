# SPDX-License-Identifier: Apache-2.0

"""The step-level column expressions, written once and used from two places.

The ``steps`` / ``tool_calls`` / ``tool_results`` views turn ATIF step JSON
into typed columns. The same columns are written to the columnar artifacts at
materialize time. Both readers must agree byte for byte, so the expressions
live here and each caller supplies only how it reaches a step's members:

* The registry's JSON path has one ``step`` JSON column holding the whole
  step, so a member is ``json_extract(step, '$.<member>')``.
* The columnar producer receives one JSON column per top-level step member
  (``step_id``, ``timestamp``, ``message``, ...), so the same member is
  ``json_extract(<member>, '$')``.

Everything below the member boundary is identical text: the message
flattener, the coalesced booleans, the token casts, and the per-call and
per-result projections (a ``call`` or ``res`` column always holds one whole
tool call or observation result as JSON on both paths).

Pure string building. No duckdb import; the callers own the connection.
Every expression comes back typed :data:`~atif_duck.domain.sql_literal.SqlFragment`:
this module is one of the three producers of SQL text (with the catalog and
``sql_literal``), and nothing here reads a value from outside the process.
"""

from __future__ import annotations

from dataclasses import dataclass

from atif_duck.domain.sql_literal import SqlFragment


@dataclass(frozen=True, slots=True)
class StepMemberAccess:
    """How to spell "the JSON value of top-level step member ``name``" in SQL.

    ``column`` names the SQL column that holds the JSON; ``path_prefix`` is
    the JSONPath that reaches the member inside it (``'$.'`` when the column
    holds a whole step, ``'$'`` when the column already is the member, in
    which case ``name`` is the column itself).
    """

    #: Column holding the whole step, or ``None`` when each member is its own column.
    whole_step_column: str | None

    def json(self, member: str, path: str = "") -> SqlFragment:
        """SQL for ``json_extract`` of ``member`` (optionally a sub-path below it)."""
        column, root = self._locate(member)
        return SqlFragment(f"json_extract({column}, '{root}{path}')")

    def string(self, member: str, path: str = "") -> SqlFragment:
        """SQL for ``json_extract_string`` of ``member`` (optionally a sub-path)."""
        column, root = self._locate(member)
        return SqlFragment(f"json_extract_string({column}, '{root}{path}')")

    def type_of(self, member: str) -> SqlFragment:
        """SQL for ``json_type`` of ``member``."""
        column, root = self._locate(member)
        return SqlFragment(f"json_type({column}, '{root}')")

    def _locate(self, member: str) -> tuple[str, str]:
        if self.whole_step_column is not None:
            return self.whole_step_column, f"$.{member}"
        return member, "$"


#: The registry's JSON path: ``UNNEST(t.steps) AS s(step)`` yields ``step``.
WHOLE_STEP: StepMemberAccess = StepMemberAccess(whole_step_column="step")

#: The producer's path: one JSON column per member, named after the member.
MEMBER_COLUMNS: StepMemberAccess = StepMemberAccess(whole_step_column=None)

#: Top-level step members the ``steps`` view reads. ``tool_calls`` and
#: ``observation`` are deliberately absent: they feed the other two views.
STEP_MEMBERS: tuple[str, ...] = (
    "step_id",
    "timestamp",
    "source",
    "model_name",
    "message",
    "metrics",
    "llm_call_count",
    "extra",
)


def step_columns(access: StepMemberAccess) -> tuple[tuple[str, str], ...]:
    """``(expression, alias)`` pairs for every ``steps`` column after ``session_id``.

    Column semantics a transcript-shaped reader would misread:

    * ``message`` is flattened text: ATIF ``Step.message`` is
      ``str | ContentPart[]``; the ARRAY branch joins the parts' ``text``
      fields with blank lines (mirrors harbor's own text bundling).
    * ``prompt_tokens`` is the ATIF TOTAL (input + cache_read +
      cache_creation); ``cached_tokens`` is the cache-read subset;
      ``cache_creation`` is dug out of ``metrics.extra`` because harbor only
      preserves the creation split there (fidelity gap 6). The two adapters
      spell that key differently (``cache_creation_input_tokens`` for Claude
      Code, ``cache_write_input_tokens`` for Codex), so the column coalesces
      them: one name reaches SQL, and a cost query does not branch on which
      agent wrote the session.
    * ``is_sidechain`` / ``is_compact_summary`` / ``source_uuids`` come from
      ``step.extra`` per the CONTRACT enrichment pass.
    """
    message_parts = access.json("message", "[*].text")
    return (
        (f"{access.json('step_id')}::BIGINT", "step_id"),
        (f"{access.string('timestamp')}::TIMESTAMP", "ts"),
        (access.string("source"), "source"),
        (access.string("model_name"), "model_name"),
        (
            (
                f"CASE WHEN {access.type_of('message')} = 'ARRAY' "
                f"THEN array_to_string(list_transform({message_parts}, "
                "part -> json_extract_string(part, '$')), '\n\n') "
                f"ELSE {access.string('message')} END"
            ),
            "message",
        ),
        (f"coalesce({access.json('extra', '.is_sidechain')}::BOOLEAN, false)", "is_sidechain"),
        (
            f"coalesce({access.json('extra', '.is_compact_summary')}::BOOLEAN, false)",
            "is_compact_summary",
        ),
        (f"{access.json('metrics', '.prompt_tokens')}::BIGINT", "prompt_tokens"),
        (f"{access.json('metrics', '.completion_tokens')}::BIGINT", "completion_tokens"),
        (f"{access.json('metrics', '.cached_tokens')}::BIGINT", "cached_tokens"),
        (
            (
                f"coalesce({access.json('metrics', '.extra.cache_creation_input_tokens')}, "
                f"{access.json('metrics', '.extra.cache_write_input_tokens')})::BIGINT"
            ),
            "cache_creation",
        ),
        (f"{access.json('llm_call_count')}::BIGINT", "llm_call_count"),
        (access.json("extra", ".source_uuids"), "source_uuids"),
    )


def step_key_columns(access: StepMemberAccess) -> tuple[tuple[str, str], ...]:
    """The ``(step_id, ts)`` pair every tool call and result row carries."""
    return (
        (f"{access.json('step_id')}::BIGINT", "step_id"),
        (f"{access.string('timestamp')}::TIMESTAMP", "ts"),
    )


#: Per-call columns over a ``call`` JSON column holding one ATIF ``ToolCall``.
#: Renamed off the ATIF field names: function_name -> tool_name,
#: tool_call_id -> tool_use_id, arguments -> tool_input. ``tool_input`` is an
#: already parsed JSON value, NOT a JSON string, so a query must not
#: json_extract it twice.
CALL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("json_extract_string(call, '$.function_name')", "tool_name"),
    ("json_extract_string(call, '$.tool_call_id')", "tool_use_id"),
    ("json_extract(call, '$.arguments')", "tool_input"),
)

#: Per-result columns over a ``res`` JSON column holding one ATIF
#: ``ObservationResult``. ``source_call_id`` is ATIF's join key back to the
#: tool_calls array, surfaced under the SAME name ``tool_use_id`` that
#: ``tool_calls`` exposes, so the two views join with ``USING (tool_use_id)``.
RESULT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("json_extract_string(res, '$.source_call_id')", "tool_use_id"),
    ("json_extract(res, '$.content')", "content"),
)


def render(columns: tuple[tuple[str, str], ...]) -> SqlFragment:
    """Join ``(expression, alias)`` pairs into a SELECT list body."""
    return SqlFragment(",\n    ".join(f"{expression} AS {alias}" for expression, alias in columns))


__all__ = [
    "CALL_COLUMNS",
    "MEMBER_COLUMNS",
    "RESULT_COLUMNS",
    "STEP_MEMBERS",
    "WHOLE_STEP",
    "StepMemberAccess",
    "render",
    "step_columns",
    "step_key_columns",
]
