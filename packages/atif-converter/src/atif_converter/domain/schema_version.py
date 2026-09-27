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
"""

from __future__ import annotations

from typing import Final

CONVERTER_SCHEMA_VERSION: Final = 1

__all__ = ["CONVERTER_SCHEMA_VERSION"]
