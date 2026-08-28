# SPDX-License-Identifier: Apache-2.0

"""Pure Leiden+CPM + mutual-kNN community-detection math (no I/O).

Pure graph + partition math; no corpus path or model id reaches here. These
functions operate on in-memory numpy matrices and plain-scalar
hyperparameters and return in-memory results — no DuckDB connection, no
parquet, no LanceDB. The orchestration that loads centroids, projects
settings into a ``CommunityConfig``, and writes the primary/sidecar parquet
lives in ``application.use_cases.community``.

The graph representation is ``WeightedGraph``, a plain immutable
(n_nodes, edges, weights) triple with no third-party graph object inside it.
Every Leiden call funnels through the single ``_leiden_membership`` seam, so
swapping the solver is one function body rather than a sweep across the
module. ``graspologic-native`` backs it today; scipy supplies the
connectivity check.

The resolution profile is a bisection over γ following Traag, Krings & Van
Dooren (2013), "Significant scales in community structure", Sci Rep 3, 2930
(DOI 10.1038/srep02930): split the γ interval wherever the community count at
the two endpoints disagrees, then keep the lowest γ per distinct community
count.

Candidate intervals are split widest-first from a priority queue rather than
depth-first. A depth-first walk spends the whole call budget inside the lowest
sub-interval it descends into and never reaches the upper half of the range,
which makes ``_pick_zoom`` select from a profile that covers a fraction of the
requested γ span. Widest-first spreads the same budget evenly, so every
truncation point leaves only intervals narrower than the ones already split.

Determinism: ``seed`` flows into every ``_leiden_membership`` call, including
the ones the bisection makes, and the bisection itself is a deterministic
interval split with no RNG — the queue breaks equal-width ties on (lo, hi).
Same seed + same input ⇒ byte-identical output across runs and across
processes. Cluster IDs are made stable by relabeling communities by descending
size after detection.

Known limits of this backend
----------------------------

Recovery of planted structure is slightly worse than the GPL stack this
replaced (igraph + leidenalg), and that was accepted as the price of dropping
copyleft. Measured across 108 pairs — 6 shapes x 3 seeds x 2 spreads x 3
resolution levels — as mean ARI against planted ground truth:

- coarse: 0.6802 native vs 0.7417 leidenalg (native better on 6, worse on 20,
  tied on 10)
- medium: 0.2445 native vs 0.2538 leidenalg (better on 8, worse on 26, tied 2)
- fine: 0.0000 both, identical on all 36
- overall: 0.3083 native vs 0.3318 leidenalg (better on 14, worse on 46,
  tied 48)

The gap is in the solver, not in γ selection: holding γ fixed at 0.05 / 0.1 /
0.2 takes the bisection out of the loop entirely and the same gap remains,
0.3813 native vs 0.4351 leidenalg over 45 pairs, native worse on 36. It is
also not the widest-first queue: that fix went in before these numbers were
taken.

The exact means move with the shape set — an independent re-run over a
different set of six shapes gave coarse 0.7107 vs 0.7500, medium 0.2473 vs
0.2567, fine identical, overall 0.3194 vs 0.3356, and native worse on 39 of 54
fixed-γ pairs — so read the sign and the magnitude, not the digits. Every run
agrees that native is a little worse at coarse and medium, identical at fine,
and never better on average. The comparison is re-runnable: install igraph and
leidenalg into a scratch environment and score both seams against the same
planted truth. Neither may be re-declared as a dependency here.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import numpy as np
from loguru import logger

if TYPE_CHECKING:
    from scipy.sparse import csr_matrix

#: Community id used for singleton / unclusterable sessions.  -1 is an obvious
#: out-of-band sentinel that stays negative even after ``Int32`` serialization.
NOISE_COMMUNITY_ID: int = -1

#: Resolution preset literal; agents pass ``--resolution {coarse, medium, fine}``
#: and the orchestrator picks γ from the resolution profile accordingly.
ResolutionLevel = Literal["coarse", "medium", "fine"]

#: Substituted for a non-positive ``n_iterations``.  "Iterate to stability" is
#: not expressible in the solver seam: it takes an unsigned count of full
#: Leiden cycles and rejects negatives.  Swept 1/2/3/4/6/10 cycles over four
#: planted-block mutual-kNN graphs (n = 120..300) at seven γ each: every one of
#: the 28 combinations reached its best CPM quality by cycle 3, and 4 keeps
#: headroom over that observed ceiling at roughly 2x the single-cycle cost.
LEIDEN_CYCLES_FOR_UNBOUNDED: int = 4

#: γ must be strictly positive: CPM at γ=0 has no null term, so every graph
#: collapses to one community and the native solver rejects the value outright.
_MIN_RESOLUTION: float = 1e-9

#: Bisection stops splitting an interval narrower than this.  Resolving to 1e-4
#: costs 898 Leiden calls at n=120 and over 4000 at n=1000; 5e-3 keeps the whole
#: dyadic grid inside the derived call budget, so the profile is complete rather
#: than truncated and the change-points still land within 0.005 of their true γ.
_PROFILE_GAMMA_TOLERANCE: float = 5e-3

#: Depth cap, a second brake independent of the tolerance.
_PROFILE_MAX_DEPTH: int = 24

#: Safety ceiling on the derived call budget, for a configured γ range wide
#: enough that the dyadic grid below outgrows any sane wall clock.  It is not
#: the bound that governs the default range -- ``_profile_call_budget`` derives
#: a smaller, provable one there.
_PROFILE_MAX_CALLS: int = 512

#: Fewest nodes a mutual-kNN graph can have and still hold an edge. Below this
#: there is no pair to be mutual about, so the build returns empty rather than
#: handing the solver a graph with no edges.
_MIN_KNN_NODES: int = 2

#: Fewest communities a partition must have for ``medium`` / ``coarse`` to treat
#: it as a scale rather than as the single-blob end of the profile.
_MIN_COMMUNITIES: int = 2

#: Fewest members a community needs before its induced subgraph is worth a
#: connectivity check: one or two nodes are connected by construction, so a
#: warning about them would carry no information.
_MIN_CONNECTIVITY_MEMBERS: int = 3


def _profile_call_budget(range_lo: float, range_hi: float) -> int:
    """Leiden calls the bisection may spend over ``[range_lo, range_hi]``.

    Every γ the bisection evaluates is an endpoint or a midpoint of a halved
    interval, so all of them land on the dyadic grid of the initial span. Two
    brakes bound how fine that grid gets: an interval is not split once its
    width falls to ``_PROFILE_GAMMA_TOLERANCE``, and not once its depth reaches
    ``_PROFILE_MAX_DEPTH``. The finest reachable spacing is therefore
    ``span / 2**k``, which caps the number of *distinct* γ — and the bisection
    only spends a call on a distinct γ — at ``2**k + 1``.

    A budget equal to that count cannot truncate a profile: the queue drains
    before the budget can bind. Over the default range that is 257 calls, and
    the widest-first sweep drains in at most 254 across planted-block shapes
    from n=60 to n=8000. ``_PROFILE_MAX_CALLS`` then clamps the result, which
    only binds for a configured range wide enough that the grid itself is the
    problem.
    """
    span = range_hi - range_lo
    levels = math.ceil(math.log2(span / _PROFILE_GAMMA_TOLERANCE))
    levels = min(max(levels, 0), _PROFILE_MAX_DEPTH)
    return min(int(2**levels) + 1, _PROFILE_MAX_CALLS)


@dataclass(frozen=True, slots=True)
class WeightedGraph:
    """Undirected weighted graph as node count + parallel edge/weight lists.

    Nodes are the integers ``0 .. n_nodes - 1``; a node may carry no edges.
    ``edges[i]`` is a ``(u, v)`` pair with ``u < v`` and ``weights[i]`` is its
    weight.  No duplicate pair appears twice.
    """

    n_nodes: int
    edges: tuple[tuple[int, int], ...]
    weights: tuple[float, ...]


def _build_mutual_knn(
    sim: np.ndarray, *, k: int, floor: float
) -> tuple[list[tuple[int, int]], list[float]]:
    """Build a mutual-kNN edge list from a precomputed symmetric similarity matrix.

    Returns parallel ``(edges, weights)`` with each edge as a sorted
    ``(u, v)`` pair (u < v), the whole list in ascending upper-triangle
    order, and no pair repeated.  Weight equals ``sim[u, v]`` directly: the
    input matrix is symmetric so ``max(w_ij, w_ji)`` is a no-op.

    The emitted order is part of the contract, not an accident: Leiden's
    local-moving queue is seeded in edge order, so a shuffled edge list with
    the same seed yields a different partition.

    ``sim`` must have its diagonal zeroed before being passed in; otherwise
    every node would pick itself as a top-k neighbor and the floor filter
    might let those through.
    """
    n = sim.shape[0]
    if n < _MIN_KNN_NODES:
        return [], []
    k_eff = min(k, n - 1)

    # argpartition returns top-k indices per row; we don't care about order
    # inside the top-k slice because the mutual filter is symmetric.
    top = np.argpartition(-sim, kth=k_eff - 1, axis=1)[:, :k_eff]

    # Build a boolean N×N "is-in-my-top-k" mask, then AND with its transpose
    # to get the mutual-kNN adjacency.
    in_top = np.zeros((n, n), dtype=bool)
    rows = np.repeat(np.arange(n), k_eff)
    cols = top.reshape(-1)
    in_top[rows, cols] = True
    mutual = in_top & in_top.T

    # Apply edge floor.
    weighted = np.where(mutual & (sim >= floor), sim, 0.0)

    # Take upper triangle to avoid duplicates.
    iu, ju = np.triu_indices(n, k=1)
    keep = weighted[iu, ju] > 0.0
    # ``ndarray.tolist()`` is typed Any; coerce elementwise so the declared
    # return type is actually established rather than asserted.
    edges: list[tuple[int, int]] = [
        (int(i), int(j)) for i, j in zip(iu[keep].tolist(), ju[keep].tolist(), strict=True)
    ]
    weights: list[float] = [float(w) for w in weighted[iu, ju][keep].tolist()]
    return edges, weights


def _build_graph(n_nodes: int, edges: list[tuple[int, int]], weights: list[float]) -> WeightedGraph:
    """Freeze an edge list into a ``WeightedGraph``."""
    if len(edges) != len(weights):
        msg = f"edges/weights length mismatch: {len(edges)} vs {len(weights)}"
        raise ValueError(msg)
    return WeightedGraph(n_nodes=n_nodes, edges=tuple(edges), weights=tuple(weights))


def _leiden_membership(
    graph: WeightedGraph, *, gamma: float, seed: int, n_iterations: int
) -> list[int]:
    """Partition ``graph`` with Leiden under CPM at resolution ``gamma``.

    This is the one place a Leiden solver is called. Any replacement backend
    (networkx's native Leiden, a vendored implementation) satisfies the same
    contract:

    - Returns a length-``n_nodes`` list of non-negative community ids, indexed
      by node.
    - Ids are dense from 0 upward but carry no ordering meaning;
      ``_relabel_and_collapse`` assigns the ids that reach the caller.
    - Every node with no edges gets its own distinct community, never a shared
      leftover bucket, so ``min_size`` collapses them to noise individually.
    - Identical ``(graph, gamma, seed, n_iterations)`` gives an identical list
      in the same process and in a fresh one.
    - ``gamma`` must be strictly positive; ``n_iterations <= 0`` means
      "iterate to stability" and maps to ``LEIDEN_CYCLES_FOR_UNBOUNDED``.
    """
    if gamma <= _MIN_RESOLUTION:
        msg = (
            f"CPM resolution must be strictly positive, got {gamma!r}; "
            f"widen leiden_resolution_range_lo above {_MIN_RESOLUTION} instead"
        )
        raise ValueError(msg)
    cycles = n_iterations if n_iterations > 0 else LEIDEN_CYCLES_FOR_UNBOUNDED

    if not graph.edges:
        return list(range(graph.n_nodes))

    import graspologic_native as gn

    native_edges = [
        (str(u), str(v), float(w)) for (u, v), w in zip(graph.edges, graph.weights, strict=True)
    ]
    # graspologic_native is a compiled abi3 extension re-exported through a
    # star-import and ships no py.typed, so neither checker can see its members.
    _quality, partition = gn.leiden(  # ty: ignore[unresolved-attribute]  # pyright: ignore[reportAttributeAccessIssue]
        edges=native_edges,
        resolution=gamma,
        use_modularity=False,
        seed=seed,
        iterations=cycles,
    )

    labels = [-1] * graph.n_nodes
    for node_str, cid in partition.items():
        labels[int(node_str)] = int(cid)

    next_id = max(labels) + 1
    for node_idx, cid in enumerate(labels):
        if cid < 0:
            labels[node_idx] = next_id
            next_id += 1
    return labels


def _cpm_quality(graph: WeightedGraph, labels: list[int], *, gamma: float) -> float:
    """CPM objective of ``labels`` on ``graph``, counting each edge twice.

    ``Q = 2 * Σ_c (e_c - γ · n_c(n_c-1)/2)`` where ``e_c`` is the total
    intra-community edge weight and ``n_c`` the community's node count. The
    factor of two is what puts the value in the same units the ``quality``
    column already carries.
    """
    intra: dict[int, float] = defaultdict(float)
    for (u, v), w in zip(graph.edges, graph.weights, strict=True):
        if labels[u] == labels[v]:
            intra[labels[u]] += w

    sizes: dict[int, int] = defaultdict(int)
    for cid in labels:
        sizes[cid] += 1

    total = 0.0
    for cid, n_c in sizes.items():
        total += intra.get(cid, 0.0) - gamma * n_c * (n_c - 1) / 2.0
    return 2.0 * total


def _compute_resolution_profile(
    graph: WeightedGraph,
    *,
    range_lo: float,
    range_hi: float,
    seed: int,
    n_iterations: int = -1,
) -> list[tuple[float, int, float, int]]:
    """Bisect γ over ``[range_lo, range_hi]`` to find community-count change-points.

    Returns rows ``(gamma, n_communities, quality, plateau_length)`` ordered
    by γ ascending with one row per distinct community count — the lowest γ
    that produces it. ``plateau_length`` is the width, in units of 1e-4, of the
    run of *evaluated* γ that share that row's community count, so it is a
    measurement rather than an extrapolation and a plateau can only be as long
    as the evidence behind it.

    The call budget comes from ``_profile_call_budget``, which derives it from
    the requested range so the dyadic grid drains before the budget binds.
    Candidate intervals are still split widest-first, so if the clamp in that
    function ever does bind, the cut-off removes the narrowest unresolved
    intervals rather than the whole upper range, and the unexplored span is
    logged: a truncated profile is otherwise indistinguishable from a complete
    one downstream, and the missing span is exactly where ``_pick_zoom`` looks.

    Row count tracks how much of the range got explored, not just how many
    distinct community counts exist: over γ ∈ [0.05, 0.95] on planted-block
    mutual-kNN graphs this returns 83 rows at n=120, 203 at n=1000 and 237 at
    n=8000, in 0.5s, 4.3s and 42s respectively. A profile that returns only a
    few dozen rows on a graph this size has been truncated rather than
    deduplicated.

    Bisection is deterministic: no RNG, and ``seed`` keys every Leiden call.
    """
    if range_hi <= range_lo:
        msg = f"empty γ range: [{range_lo}, {range_hi}]"
        raise ValueError(msg)

    budget = _profile_call_budget(range_lo, range_hi)
    evaluated: dict[float, tuple[int, float]] = {}
    calls = 0

    def evaluate(gamma: float) -> int:
        nonlocal calls
        hit = evaluated.get(gamma)
        if hit is not None:
            return hit[0]
        labels = _leiden_membership(graph, gamma=gamma, seed=seed, n_iterations=n_iterations)
        calls += 1
        n_comm = len(set(labels))
        evaluated[gamma] = (n_comm, _cpm_quality(graph, labels, gamma=gamma))
        return n_comm

    # (-width, lo, hi, n_lo, n_hi, depth): negated width makes heapq pop the
    # widest interval, and (lo, hi) breaks equal-width ties deterministically.
    # FIFO ordering measures identically (largest coverage gap 0.0563 at every
    # squeezed budget, against 0.45 for narrowest-first and deepest-first),
    # because halving every interval makes arrival order already near-descending
    # in width. Width is asserted as the explicit invariant rather than left as
    # a consequence of arrival order, which a non-halving split rule would break.
    pending: list[tuple[float, float, float, int, int, int]] = []

    def offer(lo: float, hi: float, n_lo: int, n_hi: int, depth: int) -> None:
        if n_lo == n_hi or depth >= _PROFILE_MAX_DEPTH:
            return
        if hi - lo <= _PROFILE_GAMMA_TOLERANCE:
            return
        heapq.heappush(pending, (-(hi - lo), lo, hi, n_lo, n_hi, depth))

    offer(range_lo, range_hi, evaluate(range_lo), evaluate(range_hi), 0)
    while pending and calls < budget:
        _neg_width, lo, hi, n_lo, n_hi, depth = heapq.heappop(pending)
        mid = (lo + hi) / 2.0
        n_mid = evaluate(mid)
        offer(lo, mid, n_lo, n_mid, depth + 1)
        offer(mid, hi, n_mid, n_hi, depth + 1)

    if pending:
        logger.warning(
            "Resolution profile exhausted its {}-call budget with {} interval(s) "
            "unsplit spanning γ [{:.4f}, {:.4f}] (widest {:.4f}); the profile is "
            "truncated and change-points inside that span are missing.",
            budget,
            len(pending),
            min(interval[1] for interval in pending),
            max(interval[2] for interval in pending),
            max(interval[2] - interval[1] for interval in pending),
        )

    grid = sorted(evaluated)
    counts = [evaluated[gamma][0] for gamma in grid]
    first_index_per_count: dict[int, int] = {}
    for i, n_comm in enumerate(counts):
        first_index_per_count.setdefault(n_comm, i)

    rows: list[tuple[float, int, float, int]] = []
    for i in sorted(first_index_per_count.values()):
        gamma, n_comm = grid[i], counts[i]
        plateau_end = gamma
        for j in range(i + 1, len(grid)):
            plateau_end = grid[j]
            if counts[j] != n_comm:
                break
        rows.append(
            (gamma, n_comm, evaluated[gamma][1], max(0, round((plateau_end - gamma) * 10000)))
        )
    return rows


def _pick_zoom(
    profile: list[tuple[float, int, float, int]],
    level: ResolutionLevel,
    *,
    n_nodes: int | None = None,
) -> float:
    """Pick a γ from the precomputed resolution profile.

    - ``medium`` -> γ at the longest plateau that carries actual structure.
    - ``coarse`` -> γ at the lowest n_communities plateau (with n ≥ 2).
    - ``fine``   -> γ at the highest n_communities plateau.

    Falls back to the median γ in the profile when the requested level can't
    be served (e.g., ``coarse`` when every partition has only one community).

    ``medium`` needs ``n_nodes`` to reject the degenerate ends of the profile.
    A single blob and a near-total shatter are both stable across a wide γ span
    and so tend to own the longest plateau, yet neither is a scale. Requiring at
    least two communities and at most ``n_nodes / 2`` — i.e. a mean community
    size of two or more — removed every near-shatter pick across 30 planted-block
    graphs (10 of 30 without it, mean ARI-vs-truth 0.226 -> 0.296) and stops
    ``medium`` from collapsing onto the γ that ``fine`` picks.
    """
    if not profile:
        msg = "empty resolution profile - cannot pick γ"
        raise RuntimeError(msg)

    if level == "medium":
        pool = (
            [row for row in profile if _MIN_COMMUNITIES <= row[1] <= n_nodes / 2]
            if n_nodes
            else list(profile)
        )
        # Longest plateau wins; ties broken by smallest γ for stability.
        return max(pool or profile, key=lambda row: (row[3], -row[0]))[0]

    if level == "coarse":
        # Lowest n_communities partition with at least _MIN_COMMUNITIES communities.
        eligible = [(i, row) for i, row in enumerate(profile) if row[1] >= _MIN_COMMUNITIES]
        if not eligible:
            return profile[len(profile) // 2][0]
        idx = min(eligible, key=lambda pair: (pair[1][1], pair[1][0]))[0]
        return profile[idx][0]

    # fine
    idx = max(range(len(profile)), key=lambda i: (profile[i][1], -profile[i][0]))
    return profile[idx][0]


def _run_leiden_cpm(
    graph: WeightedGraph, *, gamma: float, seed: int, n_iterations: int
) -> tuple[list[int], float]:
    """Run Leiden+CPM at ``gamma``; return ``(membership, quality)``."""
    labels = _leiden_membership(graph, gamma=gamma, seed=seed, n_iterations=n_iterations)
    return labels, _cpm_quality(graph, labels, gamma=gamma)


def _adjacency_csr(graph: WeightedGraph) -> csr_matrix:
    """Symmetric ``scipy.sparse`` CSR adjacency of ``graph``."""
    from scipy.sparse import csr_matrix

    if graph.edges:
        rows_u = np.fromiter((u for u, _ in graph.edges), dtype=np.int32, count=len(graph.edges))
        rows_v = np.fromiter((v for _, v in graph.edges), dtype=np.int32, count=len(graph.edges))
        data = np.asarray(graph.weights, dtype=np.float64)
    else:
        rows_u = np.empty(0, dtype=np.int32)
        rows_v = np.empty(0, dtype=np.int32)
        data = np.empty(0, dtype=np.float64)

    return csr_matrix(
        (
            np.concatenate([data, data]),
            (np.concatenate([rows_u, rows_v]), np.concatenate([rows_v, rows_u])),
        ),
        shape=(graph.n_nodes, graph.n_nodes),
    )


def _warn_disconnected(graph: WeightedGraph, labels: list[int]) -> None:
    """Log a warning for any community whose induced subgraph is disconnected.

    Park et al. (2024) reported up to 16% of Leiden communities disconnected
    on biomedical citation graphs.  On symmetric mutual-kNN over normalized
    cosine centroids the rate is materially lower, so we only warn -- no
    splitting.  If this fires on the live corpus regularly, bring in the
    Park et al. Connectivity Modifier as a focused follow-up.
    """
    from scipy.sparse.csgraph import connected_components

    by_cid: dict[int, list[int]] = defaultdict(list)
    for node_idx, cid in enumerate(labels):
        if cid >= 0:
            by_cid[cid].append(node_idx)
    if not any(len(nodes) >= _MIN_CONNECTIVITY_MEMBERS for nodes in by_cid.values()):
        return

    adjacency = _adjacency_csr(graph)
    for cid, nodes in by_cid.items():
        if len(nodes) < _MIN_CONNECTIVITY_MEMBERS:
            continue
        idx = np.asarray(nodes, dtype=np.int32)
        sub = adjacency[idx][:, idx]
        n_components, component_labels = connected_components(sub, directed=False)
        if n_components > 1:
            comp_sizes = sorted(np.bincount(component_labels).tolist(), reverse=True)
            logger.warning(
                "Community {} has {} weakly-connected components (sizes={}); "
                "consider rerunning with a different gamma or filing an issue if "
                "this fires regularly.",
                cid,
                n_components,
                comp_sizes,
            )


def _compute_medoid_and_coherence(
    sim: np.ndarray, labels: list[int]
) -> tuple[set[int], dict[int, float]]:
    """Per community: medoid node index + mean intra-community cosine.

    The medoid is the node with the highest mean cosine similarity to the
    other community members.  Communities of size 1 have themselves as
    medoid and coherence 1.0.  Returns the set of node indices that are
    medoids and a ``{community_id: coherence}`` map (excluding noise).
    """
    by_cid: dict[int, list[int]] = defaultdict(list)
    for node_idx, cid in enumerate(labels):
        if cid >= 0:
            by_cid[cid].append(node_idx)

    medoid_indices: set[int] = set()
    coherence: dict[int, float] = {}
    for cid, nodes in by_cid.items():
        if len(nodes) == 1:
            medoid_indices.add(nodes[0])
            coherence[cid] = 1.0
            continue
        sub = sim[np.ix_(nodes, nodes)]
        # Mean cosine to other members; subtract the diagonal contribution.
        n = len(nodes)
        per_row_sum = sub.sum(axis=1) - np.diag(sub)
        mean_to_others = per_row_sum / (n - 1)
        medoid_local = int(np.argmax(mean_to_others))
        medoid_indices.add(nodes[medoid_local])
        # Community-level coherence is the mean over the off-diagonal upper
        # triangle (==  mean pairwise cosine within the community).
        iu = np.triu_indices(n, k=1)
        coherence[cid] = float(sub[iu].mean()) if iu[0].size else 1.0
    return medoid_indices, coherence


def _relabel_and_collapse(
    raw_labels: list[int],
    sids: list[str],
    *,
    min_size: int,
    medoid_indices: set[int],
    coherence: dict[int, float],
    gamma_used: float,
) -> tuple[list[tuple[str, int, int, bool, float, float]], int, int]:
    """Stable relabel by descending size; collapse small communities to noise.

    Returns ``(rows, n_real_communities, n_noise)``.  Each row is
    ``(session_id, community_id, size, is_medoid, coherence, gamma_used)``.
    Communities below ``min_size`` get ``community_id = NOISE_COMMUNITY_ID``,
    ``is_medoid = False``, ``coherence = 0.0``.
    """
    raw_to_nodes: dict[int, list[int]] = defaultdict(list)
    for node_idx, cid in enumerate(raw_labels):
        raw_to_nodes[cid].append(node_idx)

    # Sort communities by descending size, then by smallest node index for
    # tiebreaks so the relabel is deterministic.
    sorted_raw = sorted(
        raw_to_nodes.items(), key=lambda kv: (-len(kv[1]), min(kv[1])) if kv[1] else (0, 0)
    )

    rows: list[tuple[str, int, int, bool, float, float]] = []
    new_id = 0
    n_real = 0
    n_noise = 0
    for raw_cid, nodes in sorted_raw:
        size = len(nodes)
        if size < min_size:
            for node_idx in nodes:
                rows.append((sids[node_idx], NOISE_COMMUNITY_ID, 0, False, 0.0, gamma_used))
                n_noise += 1
            continue
        n_real += 1
        comm_coherence = coherence.get(raw_cid, 0.0)
        rows.extend(
            (
                sids[node_idx],
                new_id,
                size,
                node_idx in medoid_indices,
                comm_coherence,
                gamma_used,
            )
            for node_idx in nodes
        )
        new_id += 1
    return rows, n_real, n_noise


__all__ = [
    "LEIDEN_CYCLES_FOR_UNBOUNDED",
    "NOISE_COMMUNITY_ID",
    "ResolutionLevel",
    "WeightedGraph",
    "_adjacency_csr",
    "_build_graph",
    "_build_mutual_knn",
    "_compute_medoid_and_coherence",
    "_compute_resolution_profile",
    "_cpm_quality",
    "_leiden_membership",
    "_pick_zoom",
    "_profile_call_budget",
    "_relabel_and_collapse",
    "_run_leiden_cpm",
    "_warn_disconnected",
]
