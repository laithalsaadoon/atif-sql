# SPDX-License-Identifier: Apache-2.0

"""``run_communities`` end to end over a synthetic embedding store."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, Unpack

import numpy as np
import polars as pl
import pytest

from atif_analytics.application.use_cases import community as community_mod
from atif_analytics.application.use_cases.community import run_communities
from atif_analytics.domain.structure.community import NOISE_COMMUNITY_ID, WeightedGraph
from atif_analytics.infrastructure.corpus_reader import CorpusReader

if TYPE_CHECKING:
    from atif_analytics.infrastructure.settings import AnalyticsSettings

N_PER_BLOCK = 25
N_BLOCKS = 4
N_SESSIONS = N_PER_BLOCK * N_BLOCKS


class _ProfileKwargs(TypedDict):
    """Keyword contract of ``_compute_resolution_profile``.

    ``n_iterations`` is ``NotRequired`` because the callee defaults it, so a
    spy that reads it off the call sees ``None`` when the caller omits it.
    """

    range_lo: float
    range_hi: float
    seed: int
    n_iterations: NotRequired[int]


def _planted_embeddings(seed: int = 3, dim: int = 24) -> tuple[list[str], np.ndarray]:
    """One embedding per synthetic session, drawn from four planted blocks."""
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(N_BLOCKS, dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    rows = [centers[b] + 0.3 * rng.normal(size=(N_PER_BLOCK, dim)) for b in range(N_BLOCKS)]
    matrix = np.vstack(rows).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    uuids = [f"u-{i:04d}" for i in range(N_SESSIONS)]
    return uuids, matrix


@pytest.fixture
def planted_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the use case at a planted-block store with one session per embedding."""
    uuids, matrix = _planted_embeddings()

    def _load(_uri: Path) -> tuple[list[str], np.ndarray]:
        return uuids, matrix

    def _sessions(_reader: CorpusReader) -> dict[str, str]:
        return {u: f"session-{i:04d}" for i, u in enumerate(uuids)}

    monkeypatch.setattr(community_mod, "load_embeddings", _load)
    monkeypatch.setattr(community_mod, "_uuid_to_session", _sessions)


def test_run_communities_returns_the_contract_keys_and_types(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    stats = run_communities(settings)
    assert set(stats) == {
        "sessions",
        "communities",
        "noise",
        "gamma_used",
        "quality",
        "algorithm",
        "skipped",
    }
    assert stats["skipped"] == 0
    assert stats["algorithm"] == "leiden_cpm"
    assert isinstance(stats["sessions"], int)
    assert isinstance(stats["communities"], int)
    assert isinstance(stats["noise"], int)
    assert isinstance(stats["gamma_used"], float)
    assert isinstance(stats["quality"], float)
    assert stats["sessions"] == N_SESSIONS
    assert stats["communities"] >= 1
    assert stats["communities"] + 0 <= N_SESSIONS
    assert 0.05 <= stats["gamma_used"] <= 0.95


def test_run_communities_writes_the_primary_parquet_schema(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    run_communities(settings)
    df = pl.read_parquet(settings.layout().communities_parquet)
    assert df.schema == {
        "session_id": pl.Utf8,
        "community_id": pl.Int32,
        "size": pl.Int32,
        "is_medoid": pl.Boolean,
        "coherence": pl.Float32,
        "gamma_used": pl.Float32,
    }
    assert df.height == N_SESSIONS
    real = df.filter(pl.col("community_id") != NOISE_COMMUNITY_ID)
    assert real.height > 0
    min_size = real["size"].min()
    assert isinstance(min_size, int)
    assert min_size >= 3
    # Community ids are dense from 0 after the descending-size relabel.
    ids = sorted(real["community_id"].unique().to_list())
    assert ids == list(range(len(ids)))
    # Exactly one medoid per real community.
    per_community = real.group_by("community_id").agg(pl.col("is_medoid").sum())
    assert per_community["is_medoid"].to_list() == [1] * len(ids)


def test_run_communities_relabels_by_descending_size(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    run_communities(settings)
    df = pl.read_parquet(settings.layout().communities_parquet)
    real = (
        df.filter(pl.col("community_id") != NOISE_COMMUNITY_ID)
        .group_by("community_id")
        .agg(pl.len().alias("n"))
        .sort("community_id")
    )
    sizes = real["n"].to_list()
    assert sizes == sorted(sizes, reverse=True)


def test_run_communities_writes_the_profile_sidecar(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    run_communities(settings)
    profile_path = settings.layout().community_profile_parquet
    assert profile_path.exists()
    prof = pl.read_parquet(profile_path)
    assert prof.schema == {
        "gamma": pl.Float64,
        "n_communities": pl.Int32,
        "quality": pl.Float64,
        "plateau_length": pl.Int32,
    }
    assert prof.height >= 2
    assert prof["gamma"].to_list() == sorted(prof["gamma"].to_list())


def test_run_communities_honors_an_explicit_gamma_and_skips_the_profile(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    stats = run_communities(settings, gamma=0.4)
    assert stats["gamma_used"] == pytest.approx(0.4)
    assert not settings.layout().community_profile_parquet.exists()


def test_run_communities_noise_and_real_partition_the_sessions(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    stats = run_communities(settings)
    df = pl.read_parquet(settings.layout().communities_parquet)
    noise = df.filter(pl.col("community_id") == NOISE_COMMUNITY_ID)
    assert noise.height == stats["noise"]
    assert df["session_id"].n_unique() == N_SESSIONS
    assert noise["size"].to_list() == [0] * noise.height
    assert noise["coherence"].to_list() == [0.0] * noise.height
    assert not any(noise["is_medoid"].to_list())


def test_run_communities_recovers_the_planted_blocks_at_coarse(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    """Four planted blocks, so the coarse view must not be one blob or 100 shards."""
    stats = run_communities(settings, resolution="coarse")
    assert isinstance(stats["communities"], int)
    assert 2 <= stats["communities"] <= 12
    df = pl.read_parquet(settings.layout().communities_parquet)
    real = df.filter(pl.col("community_id") != NOISE_COMMUNITY_ID)
    block_of = {f"session-{i:04d}": i // N_PER_BLOCK for i in range(N_SESSIONS)}
    labeled = real.with_columns(pl.col("session_id").replace_strict(block_of).alias("block"))
    # Purity: most nodes sit in their community's majority planted block.
    majority = (
        labeled.group_by("community_id", "block")
        .agg(pl.len().alias("n"))
        .group_by("community_id")
        .agg(pl.col("n").max().alias("best"))
    )
    assert majority["best"].sum() / labeled.height >= 0.9


def test_the_three_resolution_levels_pick_three_different_gammas(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    """coarse / medium / fine must be three scales end to end, not one γ thrice.

    ``medium`` is the production default, so a profile that collapses the levels
    onto one γ silently ignores the flag agents pass.
    """
    gammas: dict[str, float] = {}
    communities: dict[str, int] = {}
    for level in ("coarse", "medium", "fine"):
        stats = run_communities(settings, resolution=level, force=True)
        gammas[level] = float(stats["gamma_used"])
        communities[level] = int(stats["communities"])

    assert len(set(gammas.values())) == 3, gammas
    assert gammas["coarse"] < gammas["medium"] < gammas["fine"]
    assert communities["coarse"] <= communities["medium"], communities


def test_the_medium_default_is_not_pinned_to_the_range_midpoint(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    """A truncated profile pins ``medium`` on exactly the midpoint of the range."""
    cfg = settings.community_config()
    midpoint = (cfg.leiden_resolution_range_lo + cfg.leiden_resolution_range_hi) / 2.0
    stats = run_communities(settings, resolution="medium")
    assert float(stats["gamma_used"]) != pytest.approx(midpoint, abs=1e-6)


def test_the_profile_sidecar_covers_the_upper_gamma_range(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    """The γ the sidecar records must span the configured range, not just its floor."""
    run_communities(settings)
    cfg = settings.community_config()
    lo, hi = cfg.leiden_resolution_range_lo, cfg.leiden_resolution_range_hi
    midpoint = (lo + hi) / 2.0

    prof = pl.read_parquet(settings.layout().community_profile_parquet)
    gammas = prof["gamma"].to_list()
    above = [g for g in gammas if midpoint < g < hi]
    assert len(above) >= 10, f"upper range unexplored, max γ = {max(gammas)}"
    assert max(gammas) > 0.7


def test_run_communities_is_deterministic_across_runs(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    first = run_communities(settings)
    second = run_communities(settings, force=True)
    assert first == second
    df = pl.read_parquet(settings.layout().communities_parquet)
    third = run_communities(settings, force=True)
    assert third == first
    assert pl.read_parquet(settings.layout().communities_parquet).equals(df)


def test_run_communities_reuses_a_fresh_output(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    first = run_communities(settings)
    cached = run_communities(settings)
    assert cached["skipped"] == 0
    assert cached["sessions"] == first["sessions"]
    assert cached["communities"] == first["communities"]
    assert cached["gamma_used"] == pytest.approx(first["gamma_used"], abs=1e-6)


def _captured_graph(monkeypatch: pytest.MonkeyPatch) -> list[WeightedGraph]:
    """Record the ``WeightedGraph`` the use case hands to the solver."""
    seen: list[WeightedGraph] = []
    real = community_mod._build_graph

    def spy(n_nodes: int, edges: list[tuple[int, int]], weights: list[float]) -> WeightedGraph:
        graph = real(n_nodes, edges, weights)
        seen.append(graph)
        return graph

    monkeypatch.setattr(community_mod, "_build_graph", spy)
    return seen


def test_the_graph_the_use_case_builds_is_in_canonical_edge_order(
    settings: AnalyticsSettings, planted_store: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production pairing must preserve the ordering the solver depends on.

    Asserting the ordering on ``_build_mutual_knn`` in isolation leaves the
    pairing here free to reverse or shuffle the list on its way to the graph, and
    Leiden's local-moving queue is seeded in edge order.
    """
    seen = _captured_graph(monkeypatch)
    run_communities(settings, gamma=0.3)
    assert len(seen) == 1
    graph = seen[0]

    edges = list(graph.edges)
    assert edges
    assert all(u < v for u, v in edges)
    assert edges == sorted(edges)
    assert len(set(edges)) == len(edges)


def test_the_use_case_pairs_each_edge_with_its_own_similarity_weight(
    settings: AnalyticsSettings, planted_store: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Weights must stay aligned to their edge, not merely be the right multiset.

    A reversed or rotated weight list keeps every assertion about counts, sums and
    sortedness true while attaching each weight to the wrong pair.
    """
    seen = _captured_graph(monkeypatch)
    run_communities(settings, gamma=0.3)
    graph = seen[0]

    _uuids, matrix = _planted_embeddings()
    sim = (matrix @ matrix.T).astype(np.float64)
    np.fill_diagonal(sim, 0.0)

    edges = list(graph.edges)
    weights = list(graph.weights)
    assert len(edges) == len(weights)
    for (u, v), w in zip(edges, weights, strict=True):
        assert w == pytest.approx(sim[u, v], abs=1e-6)


def test_the_profile_receives_the_configured_iteration_count(
    settings: AnalyticsSettings, planted_store: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profile call must carry ``leiden_n_iterations``, not fall back to a default.

    ``_compute_resolution_profile`` defaults ``n_iterations`` to -1, which is also
    the configured value, so dropping the keyword leaves every output identical and
    every other test green. Reading the keyword off the call is the only way to see
    it, and re-running under a changed config is what proves the value flows from
    the config rather than being hardcoded.
    """
    seen: list[int | None] = []
    real = community_mod._compute_resolution_profile

    def spy(
        graph: WeightedGraph, **kwargs: Unpack[_ProfileKwargs]
    ) -> list[tuple[float, int, float, int]]:
        iterations = kwargs.get("n_iterations")
        seen.append(iterations if isinstance(iterations, int) else None)
        return real(graph, **kwargs)

    monkeypatch.setattr(community_mod, "_compute_resolution_profile", spy)

    run_communities(settings)
    cfg = settings.community_config()
    assert seen == [cfg.leiden_n_iterations]

    # A different configured value must reach the same keyword, so the assertion
    # above is not satisfied by a constant that happens to equal the default.
    monkeypatch.setattr(settings, "leiden_n_iterations", 2)
    assert settings.community_config().leiden_n_iterations == 2
    run_communities(settings, force=True)
    assert seen == [cfg.leiden_n_iterations, 2]


def test_the_configured_iteration_count_changes_the_profile_the_pipeline_writes(
    settings: AnalyticsSettings, planted_store: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing the value must alter the sidecar, not merely appear in the call.

    A spy on the keyword is satisfied by a call that passes it and a callee that
    ignores it, so the behavioral half is asserted here: one Leiden cycle finds a
    measurably different set of change-points than the configured default does.
    """
    run_communities(settings)
    default_rows = pl.read_parquet(settings.layout().community_profile_parquet).rows()

    monkeypatch.setattr(settings, "leiden_n_iterations", 1)
    run_communities(settings, force=True)
    single_cycle_rows = pl.read_parquet(settings.layout().community_profile_parquet).rows()

    assert single_cycle_rows != default_rows


def test_run_communities_quality_matches_the_cpm_formula(
    settings: AnalyticsSettings, planted_store: None
) -> None:
    """The reported quality is the CPM objective on the graph actually built."""
    from atif_analytics.domain.structure.community import (
        _build_graph,
        _build_mutual_knn,
        _cpm_quality,
        _leiden_membership,
    )

    stats = run_communities(settings, gamma=0.3)
    _uuids, matrix = _planted_embeddings()
    sim = (matrix @ matrix.T).astype(np.float64)
    np.fill_diagonal(sim, 0.0)
    cfg = settings.community_config()
    edges, weights = _build_mutual_knn(sim, k=cfg.leiden_knn_k, floor=cfg.leiden_edge_floor)
    graph = _build_graph(matrix.shape[0], edges, weights)
    labels = _leiden_membership(
        graph, gamma=0.3, seed=cfg.seed, n_iterations=cfg.leiden_n_iterations
    )
    assert stats["quality"] == pytest.approx(_cpm_quality(graph, labels, gamma=0.3))
