# SPDX-License-Identifier: Apache-2.0

"""The session id boundary: which directory names may become a session.

A session id is the one piece of text from OUTSIDE this workspace that ends up
inside a corpus path: it is derived from a transcript filename under the
source root, becomes the ``sessions/<session_id>/`` directory name, and from
there names the files atif-duck opens. Everything else in a statement is a
constant. So the id is checked once, at the boundary, and a name that fails
the check is skipped and reported rather than carried any further.

The rule is deliberately narrow: an ASCII letter or digit first, then letters,
digits, ``.``, ``_`` and ``-``, at most 255 characters (the longest name most
filesystems accept). Every id both supported agents produce passes: Claude
Code names a session with a version 4 UUID, Codex with a version 7 UUID
(``019e9e38-39a6-7170-86fb-46e6f1cb1931``). Quotes, whitespace, path
separators, ``;``, ``$``, ``?`` and anything outside ASCII fail.

DELIBERATE TWIN of :mod:`atif_corpus.domain.session_id`, the same way
`sql_literal` and `embedding_guard` twin their atif-embed copies: the independence
contract forbids the two packages from importing each other, and both sides
must apply the SAME rule: the corpus writer before it writes, this registry
before it reads a corpus an older version wrote and before any session name
reaches a path. Each package's tests read the other's copy as source text and
fail if the pattern or the limit drifts.

Pure module: stdlib only.
"""

from __future__ import annotations

import re

#: The accepted shape, as a pattern string so the twin pin can compare it.
SESSION_ID_PATTERN: str = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

#: Longest accepted id, in characters.
SESSION_ID_MAX_CHARS: int = 255

_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)


def session_id_rejection(candidate: str) -> str | None:
    """Why ``candidate`` cannot be a session id, or ``None`` when it can.

    The reason is one short phrase meant for a log line; a caller that only
    needs the verdict uses :func:`is_valid_session_id`.
    """
    if len(candidate) > SESSION_ID_MAX_CHARS:
        return f"longer than {SESSION_ID_MAX_CHARS} characters"
    # fullmatch, not match: with ``$`` alone a trailing newline would pass.
    if _SESSION_ID_RE.fullmatch(candidate) is None:
        return (
            "contains a character outside [A-Za-z0-9._-] or does not start with a letter or digit"
        )
    return None


def is_valid_session_id(candidate: str) -> bool:
    """Whether ``candidate`` passes the boundary."""
    return session_id_rejection(candidate) is None


__all__ = [
    "SESSION_ID_MAX_CHARS",
    "SESSION_ID_PATTERN",
    "is_valid_session_id",
    "session_id_rejection",
]
