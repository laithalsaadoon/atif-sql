# SPDX-License-Identifier: Apache-2.0

"""c-TF-IDF per cluster, over the corpus reader's step texts.

Joins ``clusters.parquet`` to the corpus step texts on uuid (the step's
FIRST source_uuid — the same key the lance store and clusters carry), builds
one pseudo-document per cluster, and writes the top-N terms to
``cluster_terms.parquet``. The pure ``CountVectorizer`` + c-TF-IDF math
lives in :mod:`atif_analytics.domain.structure.terms`.

Freshness keys on the CLUSTERS parquet's mtime, not on this output's mere
existence: HDBSCAN re-mints ``cluster_id`` on every fit, so terms computed
against an earlier clustering label a partition that no longer exists. A
gate that only asks whether the output file is there serves exactly that
stale labelling.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import polars as pl
from loguru import logger

from atif_analytics.domain.structure.terms import compute_ctfidf
from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.freshness import (
    is_output_fresh,
    newest_mtime_ns,
    sidecar_for,
    stamp_output,
)
from atif_analytics.infrastructure.parquet_cache import MIN_PARQUET_BYTES

if TYPE_CHECKING:
    from atif_analytics.infrastructure.settings import AnalyticsSettings


def _uuid_texts(reader: CorpusReader) -> dict[str, str]:
    """``{step_uuid: text}`` over every complete session (main + sidechain).

    The embed stage covers sidechain steps too (CONTRACT-V2: "steps text
    >=32 chars (main+sidechain)"), so the join universe must match.
    """
    out: dict[str, str] = {}
    for sid in reader.session_ids():
        for step in reader.load_steps(sid):
            if step.uuid and step.text:
                out[step.uuid] = step.text
    return out


def run_terms(
    settings: AnalyticsSettings,
    *,
    force: bool = False,
    reader: CorpusReader | None = None,
) -> dict[str, int]:
    """Compute c-TF-IDF top terms per cluster; write ``cluster_terms.parquet``.

    Returns ``{"clusters": K, "terms": N, "skipped": 0|1}`` — ``skipped=1``
    marks the graceful no-clusters path (run ``cluster`` first).
    """
    layout = settings.layout()
    out = layout.cluster_terms_parquet
    clusters_path = layout.clusters_parquet

    if not clusters_path.exists() or clusters_path.stat().st_size < MIN_PARQUET_BYTES:
        logger.info("terms: clusters parquet missing at {} — skipping", clusters_path)
        return {"clusters": 0, "terms": 0, "skipped": 1}

    sidecar = sidecar_for(out, input_name="clusters")
    clusters_mtime_ns = newest_mtime_ns(clusters_path)
    if not force and is_output_fresh(out, sidecar=sidecar, input_mtime_ns=clusters_mtime_ns):
        df = pl.read_parquet(out)
        logger.info("Clusters unchanged since last terms run; reusing {}.", out)
        return {"clusters": int(df["cluster_id"].n_unique()), "terms": len(df), "skipped": 0}

    cfg = settings.terms_config()
    reader = reader if reader is not None else CorpusReader(settings.corpus_root)

    t0 = time.monotonic()
    clusters = pl.read_parquet(clusters_path).filter(pl.col("cluster_id") >= 0)
    texts = _uuid_texts(reader)
    texts_df = pl.DataFrame(
        {"uuid": list(texts.keys()), "text": list(texts.values())},
        schema={"uuid": pl.Utf8, "text": pl.Utf8},
    )
    joined = clusters.join(texts_df, on="uuid", how="inner")
    logger.info(
        "Joined {} rows clusters x step texts in {:.1f}s", len(joined), time.monotonic() - t0
    )
    if joined.height == 0:
        logger.info("terms: no cluster rows joined to step texts — skipping")
        return {"clusters": 0, "terms": 0, "skipped": 1}

    per_cluster = (
        joined.group_by("cluster_id")
        .agg(pl.col("text").str.join("\n").alias("doc"))
        .sort("cluster_id")
    )
    docs_by_class = list(
        zip(per_cluster["cluster_id"].to_list(), per_cluster["doc"].to_list(), strict=True)
    )
    logger.info("Built {} cluster pseudo-docs", len(docs_by_class))

    rows = compute_ctfidf(docs_by_class, cfg)

    out_df = pl.DataFrame(
        rows,
        schema={
            "cluster_id": pl.Int32,
            "term": pl.Utf8,
            "weight": pl.Float32,
            "rank": pl.Int32,
        },
        orient="row",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.write_parquet(out)
    stamp_output(sidecar, clusters_mtime_ns)
    logger.info(
        "Wrote {} term-rows across {} clusters in {:.1f}s",
        len(out_df),
        len(docs_by_class),
        time.monotonic() - t0,
    )
    return {"clusters": len(docs_by_class), "terms": len(out_df), "skipped": 0}


__all__ = ["run_terms"]
