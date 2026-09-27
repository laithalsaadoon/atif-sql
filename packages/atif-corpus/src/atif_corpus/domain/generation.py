# SPDX-License-Identifier: Apache-2.0

"""Pure rule: does an artifact set's recorded provenance match what this pass writes.

The watermark answers "did the SOURCE move?". It cannot answer "would this
version of the code write the same artifacts?", so a converter upgrade or a
columnar schema bump used to leave every existing session on the old output
forever. The *generation* is the second question: a small mapping of
``meta.json`` keys to the values this pass stamps (``converter_schema``, the
converter's own output version, and ``columnar_schema`` when the columnar
producer runs), and a session whose
recorded values differ is stale even when no source byte changed.

Only the EXPECTED keys are compared. A pass that does not write columnar
artifacts expects no ``columnar_schema``, so it neither re-converts a session
that has one nor one that lacks one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def generation_matches(recorded: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    """True when every expected key is recorded with exactly the expected value.

    A missing key does not match: a session materialized before a key existed
    (``columnar_schema`` on a ``--no-columnar`` session) is exactly the one the
    new generation has to reach.
    """
    return all(key in recorded and recorded[key] == value for key, value in expected.items())
