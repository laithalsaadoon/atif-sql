#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the converter's vendored model price table from litellm's public data.

    uv run scripts/update_prices.py [--ref REF]

Run by hand, never by a gate. It downloads ``model_prices_and_context_window.json``
and ``LICENSE`` from https://github.com/BerriAI/litellm at ``REF`` (a tag, branch or
commit; default ``main``), keeps only the models atif-sql's transcripts name, folds in
:data:`OVERRIDES`, and writes
``packages/atif-converter/src/atif_converter/domain/model_prices.json``. Only the data
is taken, under litellm's MIT license, whose notice travels inside the output file's
``meta`` block. litellm itself is never installed or imported.

WHAT IS KEPT. An entry survives when all of these hold:

- its ``litellm_provider`` is one the pricing fast path replicates (``anthropic``,
  ``openai``, ``bedrock``, ``bedrock_converse``);
- its ``mode`` is ``chat``, ``responses`` or ``completion`` (a transcript's model is a
  text model; image, audio, realtime and embedding entries never price a step);
- its name, after any single ``anthropic/`` / ``openai/`` / ``bedrock/`` prefix, names
  a Claude model or an OpenAI text family (``gpt-*``, ``o<n>*``, ``codex*``,
  ``chatgpt*``), Bedrock-style ids such as ``us.anthropic.claude-*`` and
  ``global.openai.gpt-*`` included.

The upstream key order is kept, because the fast path's case-insensitive index and
alias expansion are order-sensitive exactly as litellm's are, and the
``fallback_generalizations`` rules (which route an unmapped ``claude-*`` id) are copied
verbatim.

OVERRIDES price a model upstream doesn't carry yet, from the vendor's published rates.
An override is dropped, with a notice, as soon as upstream prices the same key, so a
regenerate retires it on its own; delete it from :data:`OVERRIDES` then.

Review the diff before committing it: every changed rate changes ``total_cost_usd`` for
sessions of that model the next time they're materialized.

Exit 0 on a written table, 1 when a download fails or the upstream document isn't the
shape this script expects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

UPSTREAM_REPO = "BerriAI/litellm"
TABLE_PATH = "model_prices_and_context_window.json"
LICENSE_PATH = "LICENSE"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"

OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "packages/atif-converter/src/atif_converter/domain/model_prices.json"
)

COVERED_PROVIDERS = frozenset({"anthropic", "openai", "bedrock", "bedrock_converse"})
TEXT_MODES = frozenset({"chat", "responses", "completion"})
KEY_PREFIXES = frozenset({"anthropic", "openai", "bedrock"})
MODEL_FAMILY = re.compile(r"claude|(?:^|[./])(?:gpt-|o\d|codex|chatgpt)")

#: The top-level keys of the upstream document that are not model entries.
RULES_KEY = "fallback_generalizations"
SPEC_KEY = "sample_spec"

#: The marker an override entry carries; the converter labels a session it priced.
OVERRIDE_MARKER = "atif_sql_override"

#: Per-token USD rates for bare model ids upstream doesn't price yet, in litellm's own
#: field names so the converter prices them with the same arithmetic as any entry.
#: Only models whose rates were checked against the vendor's pricing page belong here;
#: an unchecked model stays unpriced (NULL), never guessed.
OVERRIDES: dict[str, dict[str, Any]] = {
    "claude-opus-5-5": {
        "litellm_provider": "anthropic",
        "mode": "chat",
        "input_cost_per_token": 4e-06,
        "output_cost_per_token": 2e-05,
        "cache_creation_input_token_cost": 5e-06,
        "cache_creation_input_token_cost_above_1hr": 8e-06,
        "cache_read_input_token_cost": 2e-07,
        OVERRIDE_MARKER: {
            "checked": "2026-09-27",
            "sources": [
                # "$4 / MTok" input, "$20 / MTok" output, "$5 / MTok" 5m cache write,
                # "$8 / MTok" 1h cache write, "$0.20 / MTok" cache read.
                "https://platform.claude.com/docs/en/models/opus-5-5/overview",
                # "0.05x on Claude Opus 5.5" for a cache hit.
                "https://platform.claude.com/docs/en/about-claude/pricing",
            ],
        },
    },
    "claude-fable-5-1": {
        "litellm_provider": "anthropic",
        "mode": "chat",
        "input_cost_per_token": 1e-05,
        "output_cost_per_token": 5e-05,
        "cache_creation_input_token_cost": 1.25e-05,
        "cache_creation_input_token_cost_above_1hr": 2e-05,
        "cache_read_input_token_cost": 2.5e-07,
        OVERRIDE_MARKER: {
            "checked": "2026-09-27",
            "sources": [
                # "$10 / MTok" input, "$50 / MTok" output, "$12.50 / MTok" 5m cache
                # write, "$20 / MTok" 1h cache write, "$0.25 / MTok" cache read.
                "https://platform.claude.com/docs/en/models/fable-5-1/overview",
            ],
        },
    },
}


def _download(ref: str, path: str) -> bytes:
    url = RAW_URL.format(repo=UPSTREAM_REPO, ref=ref, path=path)
    if not url.startswith("https://"):  # pragma: no cover - the template is constant
        msg = f"refusing a non-https URL: {url}"
        raise ValueError(msg)
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - https only, checked above
        return response.read()


def keep(key: str, entry: dict[str, Any]) -> bool:
    """Whether one upstream entry belongs in the vendored table."""
    if entry.get("litellm_provider") not in COVERED_PROVIDERS:
        return False
    if entry.get("mode") not in TEXT_MODES:
        return False
    prefix, slash, name = key.rpartition("/")
    if slash and (prefix not in KEY_PREFIXES or "/" in prefix):
        return False
    return MODEL_FAMILY.search(name) is not None


def build(upstream: dict[str, Any], *, ref: str, digest: str, license_text: str) -> dict[str, Any]:
    """The vendored document: provenance, the routing rules, and the kept entries."""
    rules = upstream.get(RULES_KEY)
    if not isinstance(rules, dict):
        msg = f"upstream table has no {RULES_KEY!r} object"
        raise TypeError(msg)
    models: dict[str, Any] = {
        key: entry
        for key, entry in upstream.items()
        if key not in {RULES_KEY, SPEC_KEY} and isinstance(entry, dict) and keep(key, entry)
    }
    for key, entry in OVERRIDES.items():
        if key in upstream:
            sys.stderr.write(
                f"upstream now prices {key!r}; its override is dropped. "
                "Delete it from OVERRIDES in scripts/update_prices.py.\n"
            )
            continue
        models[key] = entry
    return {
        "meta": {
            "source": f"https://github.com/{UPSTREAM_REPO}/blob/{ref}/{TABLE_PATH}",
            "ref": ref,
            "sha256": digest,
            "generator": "scripts/update_prices.py",
            "license": "MIT",
            "license_text": license_text,
        },
        RULES_KEY: rules,
        "models": models,
    }


def main(argv: list[str] | None = None) -> int:
    """Download, filter, and write the vendored table."""
    parser = argparse.ArgumentParser(
        description="Regenerate the vendored model price table from litellm's public data."
    )
    parser.add_argument("--ref", default="main", help="litellm git ref to read (default: main)")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="where to write the table")
    args = parser.parse_args(argv)

    try:
        raw = _download(args.ref, TABLE_PATH)
        license_raw = _download(args.ref, LICENSE_PATH)
    except OSError as exc:
        sys.stderr.write(f"download failed: {exc}\n")
        return 1
    upstream = json.loads(raw)
    if not isinstance(upstream, dict):
        sys.stderr.write("upstream table is not a JSON object\n")
        return 1
    # Only the MIT half of litellm's LICENSE covers the data file; the enterprise
    # carve-out above it names a directory the table doesn't live in.
    license_text = license_raw.decode("utf-8")
    mit_start = license_text.find("MIT License")
    if mit_start < 0:
        sys.stderr.write("upstream LICENSE carries no MIT License section\n")
        return 1
    try:
        document = build(
            upstream,
            ref=args.ref,
            digest=hashlib.sha256(raw).hexdigest(),
            license_text=license_text[mit_start:].strip(),
        )
    except TypeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    args.output.write_text(
        json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    sys.stdout.write(f"wrote {len(document['models'])} entries from {args.ref} to {args.output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
