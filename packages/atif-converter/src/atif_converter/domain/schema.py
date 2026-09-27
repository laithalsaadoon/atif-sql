# SPDX-License-Identifier: Apache-2.0

"""The converter's own output-shape version.

``meta.converter_version`` records the installed atif-converter DISTRIBUTION
version, which moves on every release and says nothing about whether the
artifacts a release writes differ. :data:`CONVERTER_SCHEMA_VERSION` is the
number that does: bump it in the same change as anything that alters what
the converter writes for the same transcript bytes (a new ``extra`` key, a
rewritten field, a new artifact), so the corpus can tell a session converted
by an older shape and re-convert it.

History
-------
1. Everything before the version existed.
2. Inline base64 attachments replaced by blob placeholders (and the bytes
   handed out for the corpus blob store); typed ``is_error`` / ``exit_code`` /
   ``interrupted`` / ``images`` on observation results; ``agent_id`` read from
   ``agentId`` and filled on every sidechain step; ``trajectory.extra.subagents``
   from the ``agent-*.meta.json`` sidecars.
"""

from __future__ import annotations

#: Bump with any change to the converter's output for the same input bytes.
CONVERTER_SCHEMA_VERSION: int = 2

__all__ = ["CONVERTER_SCHEMA_VERSION"]
