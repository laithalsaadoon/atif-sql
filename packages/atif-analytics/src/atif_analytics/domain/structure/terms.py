# SPDX-License-Identifier: Apache-2.0

"""Pure c-TF-IDF term-weighting math (no I/O, no bertopic).

Pure c-TF-IDF math; no corpus path or model id reaches here.
``compute_ctfidf`` takes one pseudo-document per class (cluster) and a frozen
``TermsConfig`` and returns the top-N ``(cluster_id, term, weight, rank)``
rows. It performs no I/O — the DuckDB join that builds the per-cluster docs
and the parquet write live in ``application.use_cases.terms``.

We keep the weighting logic visible and patchable (per-class TF, IDF, L1
norm, ngram, ``min_df``) rather than pulling in bertopic. sklearn's
``CountVectorizer`` is lazy-imported inside the function so importing this
module doesn't drag the sklearn subtree onto the CLI fast path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

if TYPE_CHECKING:
    from atif_analytics.domain.config import TermsConfig


def compute_ctfidf(
    docs_by_class: Sequence[tuple[int, str]], cfg: TermsConfig
) -> list[tuple[int, str, float, int]]:
    """Compute top-N c-TF-IDF terms per class (cluster).

    ``docs_by_class`` is ordered ``(cluster_id, pseudo_document)`` pairs —
    one pseudo-document per cluster, sorted by ``cluster_id`` for
    deterministic output. Returns ``(cluster_id, term, weight, rank)``
    rows, ranks 1-based, terms with a non-positive weight dropped.
    """
    from sklearn.feature_extraction.text import CountVectorizer

    cluster_ids = [cid for cid, _ in docs_by_class]
    docs = [doc for _, doc in docs_by_class]

    cv = CountVectorizer(
        min_df=cfg.tfidf_min_df,
        max_df=cfg.tfidf_max_df,
        ngram_range=(cfg.tfidf_ngram_min, cfg.tfidf_ngram_max),
        lowercase=True,
        strip_accents="unicode",
    ).fit(docs)
    tf = cv.transform(docs).toarray().astype(np.float32)  # (n_clusters, vocab)
    vocab = cv.get_feature_names_out()
    logger.info("Vocabulary size: {} terms", len(vocab))

    # c-TF-IDF: term frequency in cluster x log(1 + avg_docs_per_term / col_sum).
    row_sum = tf.sum(axis=1, keepdims=True)
    row_sum[row_sum == 0] = 1.0
    tf_norm = tf / row_sum
    col_sum = tf.sum(axis=0)
    total = tf.sum()
    avg = col_sum / max(total, 1.0)
    idf = np.log(1.0 + (avg.sum() / np.maximum(col_sum, 1e-9)))
    ctfidf = tf_norm * idf  # (n_clusters, vocab)

    top_n = cfg.tfidf_top_n_terms
    rows: list[tuple[int, str, float, int]] = []
    for k, cid in enumerate(cluster_ids):
        idx = np.argsort(-ctfidf[k])[:top_n]
        for rank, i in enumerate(idx):
            w = float(ctfidf[k, i])
            if w <= 0:
                continue
            rows.append((int(cid), str(vocab[i]), w, rank + 1))
    return rows


__all__ = ["compute_ctfidf"]
