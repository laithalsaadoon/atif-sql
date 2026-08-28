# SPDX-License-Identifier: Apache-2.0

"""LIVE smoke: the four LLM pipelines over the /tmp/atif-e2e corpus.

Runs classify + friction + trajectory + conflicts with ``--no-dry-run
--limit 3`` against the real materialized corpus at ``/tmp/atif-e2e``
(terra for classify/trajectory, sol for conflicts, luna for friction — the
CONTRACT-V2 size assignments through the atif-models registry).

Costs real money (a few dollars at most for three sessions) and needs
ambient AWS creds with bedrock-runtime access in us-east-1. State +
parquet outputs land under the corpus's ``analytics/`` dir, so a re-run
skips everything via the checkpoint — pass a fresh corpus copy to re-test.

Run from the workspace root:

    uv run python scripts/dev/smoke_analytics.py [corpus_root]

Deliberately NOT a pytest test — it must never run inside the gate suite.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from atif_analytics.application.use_cases.classify import classify_sessions
from atif_analytics.application.use_cases.conflicts import detect_conflicts
from atif_analytics.application.use_cases.friction import detect_user_friction
from atif_analytics.application.use_cases.trajectory import trajectory_messages
from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.parquet_cache import ParquetCache
from atif_analytics.infrastructure.settings import AnalyticsSettings
from atif_models.domain.registry import estimate_cost
from atif_models.infrastructure.openai_bedrock import OpenAiBedrockProvider

LIMIT = 3


def _sample_row(cache: ParquetCache) -> dict[str, Any] | None:
    df = cache.read_all()
    if df is None or df.height == 0:
        return None
    return df.to_dicts()[0]


def main() -> None:
    """Run every analytics pipeline once and print estimate-vs-actual per stage."""
    corpus_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/atif-e2e")  # noqa: S108 — dev smoke default, documented in the docstring
    settings = AnalyticsSettings(corpus_root=corpus_root)
    layout = settings.layout()
    reader = CorpusReader(corpus_root, caps=settings.transcript_caps())

    pipelines = [
        ("classify", classify_sessions, layout.classifications_dir),
        ("friction", detect_user_friction, layout.user_friction_dir),
        ("trajectory", trajectory_messages, layout.trajectory_dir),
        ("conflicts", detect_conflicts, layout.conflicts_dir),
    ]

    llm = settings.llm()

    for name, fn, cache_dir in pipelines:
        # Constructed here rather than through `build_provider`, which returns the
        # `LlmStructuredProvider` PORT: the port carries no `usage`, so reading the
        # accumulator through it is unsound. This smoke exists to compare the plan's
        # estimate against the real token counts, so it needs the concrete adapter that
        # owns them. The construction is the same one build_provider performs — the size
        # per pipeline still resolves through the atif-models registry, never a model id
        # written down here.
        spec = llm.spec_for(name)
        provider = OpenAiBedrockProvider(
            spec, region=llm.llm_region, concurrency=llm.llm_concurrency
        )

        # Dry-run FIRST: the measured estimation + budget ceilings must show
        # up in the plan, and the estimate gets compared against actuals.
        plan = fn(
            settings,
            since_days=None,
            limit=LIMIT,
            dry_run=True,
            reader=reader,
            provider=provider,
            spec=spec,
        )
        if not isinstance(plan, dict):
            msg = f"{name} dry-run returned {type(plan).__name__}, expected plan dict"
            raise TypeError(msg)

        t0 = time.monotonic()
        result = fn(
            settings,
            since_days=None,
            limit=LIMIT,
            dry_run=False,
            reader=reader,
            provider=provider,
            spec=spec,
        )
        elapsed = time.monotonic() - t0
        usage = provider.usage.summary()
        cost = estimate_cost(
            spec, input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]
        )
        cache = ParquetCache(cache_dir)
        est_in = plan.get("estimated_input_tokens", 0)
        act_in = usage["input_tokens"]
        ratio = (act_in / est_in) if est_in else float("nan")
        print(f"\n=== {name} -> {spec.model_id} ===")  # noqa: T201
        print(f"  plan        : {plan}")  # noqa: T201
        print(f"  result      : {result}")  # noqa: T201
        print(f"  rows in cache: {cache.count_rows()}")  # noqa: T201
        print(f"  elapsed     : {elapsed:.1f}s")  # noqa: T201
        print(f"  usage       : {usage}")  # noqa: T201
        print(f"  est. cost   : ${plan.get('estimated_cost_usd', 0):.4f} (plan, measured)")  # noqa: T201
        print(f"  act. cost   : ${cost:.4f}" if cost is not None else "  act. cost   : n/a")  # noqa: T201
        print(f"  in-tokens   : est {est_in} vs actual {act_in} (actual/est = {ratio:.2f}x)")  # noqa: T201
        ceilings = (
            f"sessions<={plan.get('max_sessions_per_run')} "
            f"cost<=${plan.get('max_cost_usd_per_run')}"
        )
        print(f"  ceilings    : {ceilings}")  # noqa: T201
        sample = _sample_row(cache)
        print(f"  sample row  : {sample}")  # noqa: T201


if __name__ == "__main__":
    main()
