# SPDX-License-Identifier: Apache-2.0
"""Atheris harness: arbitrary bytes as a Claude Code session, through ``convert_and_audit``.

    uv run --group fuzz python fuzz/fuzz_claude_code.py [libFuzzer flags] [CORPUS_DIR ...]

``mise run fuzz:claude-code`` runs it through ``run_fuzzers.py`` with a time bound,
the seed corpus and the dictionary. The target and its contract live in
``transcript_targets.claude_code_one_input``.
"""

from __future__ import annotations

import sys

# atheris comes from the `fuzz` group (linux x86_64); without it pyright reads typeshed's stub.
import atheris  # pyright: ignore[reportMissingModuleSource]
from loguru import logger

# typeshed's atheris stub does not re-export `instrument_imports`, which atheris itself does.
with atheris.instrument_imports(  # pyright: ignore[reportAttributeAccessIssue]
    include=["atif_converter", "transcript_targets"]
):
    import transcript_targets


def main() -> None:
    """Silence the converter's debug log, then hand the target to libFuzzer."""
    logger.remove()
    atheris.Setup(sys.argv, transcript_targets.claude_code_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
