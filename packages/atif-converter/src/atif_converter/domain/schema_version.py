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
* 4 — Codex ``is_error`` / ``exit_code`` for code-mode ``exec`` scripts, from
  the script's own header and the items its nested commands, MCP calls and
  patches complete; ``is_error`` from the ``status`` of ``FileChange`` and
  ``CollabAgentToolCall`` items.
* 5 — Claude Code ``agent.extra`` lists (``cwds``, ``git_branches``,
  ``agent_ids``) in first-seen order instead of set order, so a session
  converts to the same bytes in every process.
* 6 — prices from litellm v1.103.2, whose arithmetic bills cache writes and
  cache reads at the input rate for a model that publishes no cache-creation
  or cache-read rate (every OpenAI text model's cache writes, and cached
  tokens of the 46 entries with no cache-read rate), where v1.102.0 billed
  them $0. The table also adds flex rates for gpt-5.1 and gpt-5.2 and moves
  o4-mini's flex cache-read rate. ``total_cost_usd`` and ``metrics.cost_usd``
  change for every such session.
* 7 — harbor 0.24.0: ``meta.json`` records ``harbor_version`` 0.24.0. The
  Claude Code port itself puts each event's ``agentId`` on every step the
  event becomes (prompts, tool results and turns, which enrichment filled
  before) and writes ``is_sidechain`` and ``agent_id`` right after ``id`` in
  ``extra``, harbor's key order; a snake_case ``agent_id`` key names no
  subagent (harbor #3434). A Codex tool output that is a list of content blocks or
  items is its text parts joined by newlines, with an inline image as its blob
  placeholder, where it was Python's ``str()`` of the list; a non-string
  ``output`` inside an output object is its JSON text; a JSON scalar output
  keeps its raw text (harbor #3467). A Codex ``web_search_call`` carries its
  item id, or ``<api call>_web_search_<n>``, where its id was empty (harbor
  #2972).
* 8 — every Codex tool output image the port writes a blob placeholder for is
  lifted by the pre-pass too: an MCP ``image`` block with inline base64 (by
  ``mimeType`` or ``mime_type``), and an ``input_image`` whose data URL
  carries parameters or whose base64 is wrapped. Its bytes reach the blob
  store, its result carries ``extra.images``, and a tool output list the port
  dumps as JSON (one element of an unknown type) holds the placeholder item
  where it held the base64. Placeholder text is unchanged.
* 9 — a Codex code-mode ``exec`` script's nested MCP calls and commands (the
  ``McpToolCall`` / ``CommandExecution`` items attributed to it) are tool
  calls of their own on its step, after the ``exec`` call, with
  ``extra.nested_in``, ``via`` and ``duration_ms``, and one result each
  carrying its content and its own ``is_error`` / ``exit_code`` (and
  ``images``: the pre-pass lifts images out of those items' MCP results too).
  The ``exec`` result's own fields are unchanged.
"""

from __future__ import annotations

from typing import Final

CONVERTER_SCHEMA_VERSION: Final = 9

__all__ = ["CONVERTER_SCHEMA_VERSION"]
