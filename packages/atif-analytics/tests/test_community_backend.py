# SPDX-License-Identifier: Apache-2.0

"""Community-detection backend: CPM quality, γ bisection, connectivity, determinism."""

from __future__ import annotations

import itertools
import math
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable
from typing import TypedDict, Unpack

import numpy as np
import pytest

from atif_analytics.domain.structure import community as community_mod
from atif_analytics.domain.structure.community import (
    _PROFILE_GAMMA_TOLERANCE,
    _PROFILE_MAX_CALLS,
    _PROFILE_MAX_DEPTH,
    LEIDEN_CYCLES_FOR_UNBOUNDED,
    WeightedGraph,
    _adjacency_csr,
    _build_graph,
    _build_mutual_knn,
    _compute_medoid_and_coherence,
    _compute_resolution_profile,
    _cpm_quality,
    _leiden_membership,
    _pick_zoom,
    _profile_call_budget,
    _relabel_and_collapse,
    _run_leiden_cpm,
    _warn_disconnected,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

ZOOM_LEVELS = ("coarse", "medium", "fine")


def _planted_similarity(
    n_per: int = 30, n_blocks: int = 4, *, seed: int = 0, dim: int = 16, spread: float = 0.35
) -> tuple[np.ndarray, list[int]]:
    """Cosine similarity over L2-normalized planted-block vectors + ground truth."""
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(n_blocks, dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    rows = [centers[b] + spread * rng.normal(size=(n_per, dim)) for b in range(n_blocks)]
    matrix = np.vstack(rows).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    sim = (matrix @ matrix.T).astype(np.float64)
    np.fill_diagonal(sim, 0.0)
    truth = [b for b in range(n_blocks) for _ in range(n_per)]
    return sim, truth


class _PlantedKwargs(TypedDict, total=False):
    """Keyword contract of ``_planted_similarity`` — every key is defaulted there."""

    n_per: int
    n_blocks: int
    seed: int
    dim: int
    spread: float


def _planted_graph(**kwargs: Unpack[_PlantedKwargs]) -> tuple[WeightedGraph, list[int]]:
    sim, truth = _planted_similarity(**kwargs)
    edges, weights = _build_mutual_knn(sim, k=15, floor=0.3)
    return _build_graph(sim.shape[0], edges, weights), truth


class _LeidenKwargs(TypedDict):
    """Keyword contract of ``_leiden_membership``; none of them is defaulted."""

    gamma: float
    seed: int
    n_iterations: int


def _fixed_budget(value: int) -> Callable[[float, float], int]:
    """Stand-in for ``_profile_call_budget`` that answers ``value`` for any range."""

    def budget(_lo: float, _hi: float) -> int:
        return value

    return budget


# ---------------------------------------------------------------------------
# CPM quality formula, against values computed by hand
# ---------------------------------------------------------------------------


def test_cpm_quality_two_triangles_hand_computed() -> None:
    """Two weight-1.0 triangles, each its own community, at γ=0.5.

    Per community: e_c = 3.0, n_c = 3 so the null term is 0.5·3·2/2 = 1.5,
    giving 3.0 - 1.5 = 1.5.  Two communities sum to 3.0 and the doubling
    convention makes Q = 6.0.
    """
    graph = _build_graph(
        6,
        [(0, 1), (0, 2), (1, 2), (3, 4), (3, 5), (4, 5)],
        [1.0] * 6,
    )
    assert _cpm_quality(graph, [0, 0, 0, 1, 1, 1], gamma=0.5) == pytest.approx(6.0)


def test_cpm_quality_all_in_one_community_hand_computed() -> None:
    """The same six nodes merged into one community at γ=0.5.

    e_c = 6.0 (both triangles are now intra), n_c = 6 so the null term is
    0.5·6·5/2 = 7.5.  Q = 2·(6.0 - 7.5) = -3.0: merging two disconnected
    triangles is worse than keeping them apart.
    """
    graph = _build_graph(
        6,
        [(0, 1), (0, 2), (1, 2), (3, 4), (3, 5), (4, 5)],
        [1.0] * 6,
    )
    assert _cpm_quality(graph, [0] * 6, gamma=0.5) == pytest.approx(-3.0)


def test_cpm_quality_all_singletons_is_zero() -> None:
    """Singletons have no intra edges and n_c(n_c-1)/2 = 0, so Q = 0 at any γ."""
    graph = _build_graph(4, [(0, 1), (1, 2), (2, 3)], [0.9, 0.8, 0.7])
    for gamma in (0.05, 0.5, 0.95):
        assert _cpm_quality(graph, [0, 1, 2, 3], gamma=gamma) == pytest.approx(0.0)


def test_cpm_quality_uses_edge_weights_not_edge_counts() -> None:
    """A single weighted edge inside one community: Q = 2·(w - γ·1)."""
    graph = _build_graph(2, [(0, 1)], [0.75])
    assert _cpm_quality(graph, [0, 0], gamma=0.25) == pytest.approx(2.0 * (0.75 - 0.25))


def test_cpm_quality_null_term_scales_quadratically_with_size() -> None:
    """An edgeless 5-clique-as-one-community is pure penalty: -2·γ·10."""
    graph = _build_graph(5, [], [])
    assert _cpm_quality(graph, [0] * 5, gamma=0.3) == pytest.approx(-2.0 * 0.3 * 10)


def test_cpm_quality_agrees_with_the_solvers_own_ranking() -> None:
    """The partition the solver returns must beat the all-in-one alternative."""
    graph, _truth = _planted_graph()
    labels, quality = _run_leiden_cpm(graph, gamma=0.1, seed=42, n_iterations=-1)
    assert quality == pytest.approx(_cpm_quality(graph, labels, gamma=0.1))
    assert quality > _cpm_quality(graph, [0] * graph.n_nodes, gamma=0.1)


# ---------------------------------------------------------------------------
# Resolution-profile bisection
# ---------------------------------------------------------------------------


def test_profile_rows_are_gamma_ascending_and_count_deduped() -> None:
    graph, _truth = _planted_graph()
    profile = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
    assert len(profile) >= 5
    gammas = [row[0] for row in profile]
    assert gammas == sorted(gammas)
    assert len(set(gammas)) == len(gammas)
    counts = [row[1] for row in profile]
    assert len(set(counts)) == len(counts)


def test_profile_community_count_rises_with_gamma_overall() -> None:
    """CPM splits harder as γ grows, so the first row is the coarsest."""
    graph, _truth = _planted_graph()
    profile = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
    counts = [row[1] for row in profile]
    assert counts[0] == min(counts)
    assert counts[-1] == max(counts)
    # The trend is strongly increasing but not strictly so: Leiden is a
    # heuristic, so a slightly higher γ occasionally lands one community fewer.
    rising = sum(1 for a, b in itertools.pairwise(counts) if b > a)
    assert rising >= 0.8 * (len(counts) - 1)


def test_profile_keeps_the_lowest_gamma_for_each_community_count() -> None:
    """Re-solving at a row's γ reproduces that row's count and quality."""
    graph, _truth = _planted_graph()
    profile = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
    for gamma, n_comm, quality, _plateau in profile:
        labels, requality = _run_leiden_cpm(graph, gamma=gamma, seed=42, n_iterations=-1)
        assert len(set(labels)) == n_comm
        assert requality == pytest.approx(quality)


def test_profile_plateau_lengths_are_measured_not_extrapolated() -> None:
    """Each plateau must be backed by evaluated γ, and none may run off the range.

    A plateau that extends to ``range_hi`` on faith is how a truncated profile
    manufactures a dominant plateau out of nothing, so the sum stays *under* the
    range width and every individual plateau stays inside it.
    """
    graph, _truth = _planted_graph()
    lo, hi = 0.05, 0.95
    profile = _compute_resolution_profile(graph, range_lo=lo, range_hi=hi, seed=42)
    span = round((hi - lo) * 10000)
    assert all(row[3] >= 0 for row in profile)
    assert sum(row[3] for row in profile) <= span
    # Plateaus are quantized to 1e-4, so allow one rounding step past ``hi``.
    for gamma, _n_comm, _quality, plateau in profile:
        assert gamma + plateau / 10000.0 <= hi + 1e-4

    # A plateau must stop at the next change-point, not run on to the next row
    # that survived the per-count dedup: rows are deduplicated, so the gap to the
    # next row spans every intermediate count and inflates the plateau.
    for (gamma, _n_comm, _quality, plateau), (next_gamma, *_rest) in itertools.pairwise(profile):
        assert gamma + plateau / 10000.0 <= next_gamma + 1e-4

    # A plateau ends at the first γ whose count differs, so re-solving one step
    # past it must land on a different count.  Measuring the gap to the next
    # surviving row instead spans the deduplicated counts in between and reports
    # a plateau wider than the flat region it names.
    for gamma, n_comm, _quality, plateau in profile:
        if plateau <= 0:
            continue
        past = gamma + (plateau + 1) / 10000.0
        if past >= hi:
            continue
        beyond = len(set(_leiden_membership(graph, gamma=past, seed=42, n_iterations=-1)))
        assert beyond != n_comm or plateau <= 1, (
            f"row γ={gamma} n={n_comm} claims a plateau of {plateau} but the count "
            f"is still {beyond} past its end"
        )

    # Solving across a plateau must hold the row's count; that is the claim the
    # number makes and the one a fabricated plateau fails.
    widest = max(profile, key=lambda row: row[3])
    width = widest[3] / 10000.0
    if width > 0:
        observed = [
            len(
                set(
                    _leiden_membership(graph, gamma=widest[0] + width * f, seed=42, n_iterations=-1)
                )
            )
            for f in (0.0, 0.5, 1.0)
        ]
        assert max(observed) - min(observed) <= 0.1 * widest[1] + 2, observed


def test_profile_covers_the_upper_half_of_the_requested_gamma_range() -> None:
    """The upper range must be explored, not abandoned once the budget runs low.

    A depth-first split descends the low half first and spends the entire call
    budget there, so every γ above the midpoint goes unevaluated even though the
    community count keeps climbing through it.  Gutting either half of the split
    has to fail here.
    """
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    lo, hi = 0.05, 0.95
    midpoint = (lo + hi) / 2.0
    profile = _compute_resolution_profile(graph, range_lo=lo, range_hi=hi, seed=42)

    gammas = [row[0] for row in profile]
    interior_above = [g for g in gammas if midpoint < g < hi]
    interior_below = [g for g in gammas if lo < g < midpoint]
    assert len(interior_above) >= 20, f"upper half unexplored: {sorted(gammas)[-5:]}"
    assert len(interior_below) >= 20, f"lower half unexplored: {sorted(gammas)[:5]}"
    assert max(gammas) > 0.75

    # The count really does keep rising up there, so those rows carry signal.
    above = [row for row in profile if row[0] > midpoint]
    assert max(row[1] for row in above) > min(row[1] for row in above) * 1.5


def test_profile_spreads_its_budget_instead_of_exhausting_one_subinterval() -> None:
    """Coverage must be even: no half of the range may hoard the evaluated γ.

    Depth-first put 100% of the change-points below the midpoint.  Requiring
    both halves to hold a real share of them is what makes that unbalanced.
    """
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    lo, hi = 0.05, 0.95
    midpoint = (lo + hi) / 2.0
    profile = _compute_resolution_profile(graph, range_lo=lo, range_hi=hi, seed=42)

    above = sum(1 for row in profile if row[0] > midpoint)
    share = above / len(profile)
    assert 0.25 <= share <= 0.75, f"{above}/{len(profile)} change-points above the midpoint"


def test_profile_never_abandons_a_wide_interval_while_splitting_narrow_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under truncation the coverage gap must stay near the finest resolution reached.

    This is the invariant widest-first buys and the property that ordering choice
    actually decides.  At the production budget the queue drains, so the ordering
    is unobservable there; squeezing the budget is what exposes it.  A depth-first
    or narrowest-first walk leaves a 0.45-wide hole while splitting intervals
    0.007 wide -- a 64x disparity -- whereas widest-first keeps the largest gap
    within one split of the narrowest interval it touched.

    FIFO ordering is the one substitution this does NOT catch, and measurably so:
    it produces the same 0.0563 largest gap at every squeezed budget, because
    halving every interval makes arrival order already near-descending in width.
    The explicit heap key is asserted separately below, since coverage cannot
    distinguish the two.
    """
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    lo, hi = 0.05, 0.95

    for budget in (20, 40, 80):
        monkeypatch.setattr(community_mod, "_PROFILE_MAX_CALLS", budget)
        monkeypatch.setattr(community_mod, "_profile_call_budget", _fixed_budget(budget))
        profile = _compute_resolution_profile(graph, range_lo=lo, range_hi=hi, seed=42)
        gammas = sorted(row[0] for row in profile)
        largest_gap = max(b - a for a, b in itertools.pairwise(gammas))
        # Every split halves an interval, so after k splits of the initial span
        # no gap should exceed span / 2**2 once even a handful of calls are spent.
        assert largest_gap <= (hi - lo) / 4.0, (
            f"budget={budget} left a {largest_gap:.4f}-wide hole in [{lo}, {hi}]"
        )


def test_the_pending_queue_pops_the_widest_interval_first() -> None:
    """Width must be the pop order explicitly, not a by-product of arrival order.

    FIFO coincides with widest-first on a pure halving rule, so no coverage
    assertion can separate them and the ordering has to be read off the queue
    itself. A split rule that ever produced uneven children would diverge, and
    the guard above would keep passing.
    """
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    popped: list[tuple[float, float]] = []
    real_heappop = community_mod.heapq.heappop

    def spy(heap: list[tuple[float, float, float, int, int, int]]) -> object:
        widest = -min(entry[0] for entry in heap)
        item = real_heappop(heap)
        popped.append((widest, item[2] - item[1]))
        return item

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(community_mod, "_profile_call_budget", _fixed_budget(60))
        mp.setattr(community_mod.heapq, "heappop", spy)
        _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)

    assert len(popped) > 20
    for widest_available, taken in popped:
        assert taken == pytest.approx(widest_available), (
            f"popped a {taken:.6f}-wide interval while a {widest_available:.6f}-wide one waited"
        )
    # The queue held intervals of differing width, so the assertion had a choice
    # to discriminate rather than a single candidate.
    assert len({round(w, 6) for w, _t in popped}) > 3


def test_profile_warns_and_names_the_unexplored_span_when_the_budget_runs_out(
    captured_logs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truncation must be audible; downstream cannot otherwise tell it happened."""
    monkeypatch.setattr(community_mod, "_profile_call_budget", _fixed_budget(8))
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)

    warnings = [line for line in captured_logs if "exhausted its" in line]
    assert len(warnings) == 1
    assert "truncated" in warnings[0]
    assert "unsplit spanning" in warnings[0]

    # The named span must be inside the requested range and the widest unsplit
    # interval no wider than it -- a figure read off the heap key instead of the
    # bounds prints thousands and tells the reader nothing.
    span = re.search(r"γ \[([\d.]+), ([\d.]+)\] \(widest ([\d.]+)\)", warnings[0])
    assert span is not None, warnings[0]
    low, high, widest = (float(g) for g in span.groups())
    assert 0.05 <= low < high <= 0.95
    assert 0.0 < widest <= high - low


def test_profile_stays_quiet_when_it_finishes_inside_the_budget(
    captured_logs: list[str],
) -> None:
    """The complement of the warning test: a complete profile must not cry wolf."""
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
    assert [line for line in captured_logs if "exhausted its" in line] == []


def _count_profile_calls(
    graph: WeightedGraph, *, range_lo: float, range_hi: float, seed: int = 42
) -> int:
    """Solver calls one ``_compute_resolution_profile`` spends."""
    calls = 0
    real = community_mod._leiden_membership

    def counting(solved: WeightedGraph, **kwargs: Unpack[_LeidenKwargs]) -> list[int]:
        nonlocal calls
        calls += 1
        return real(solved, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(community_mod, "_leiden_membership", counting)
        _compute_resolution_profile(graph, range_lo=range_lo, range_hi=range_hi, seed=seed)
    return calls


def _truncation_warnings(
    graph: WeightedGraph, *, range_lo: float, range_hi: float, seed: int = 42
) -> list[str]:
    """Budget-exhaustion warnings one ``_compute_resolution_profile`` emits."""
    messages: list[str] = []

    def record(template: object, *_args: object, **_kw: object) -> None:
        messages.append(str(template))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(community_mod.logger, "warning", record)
        _compute_resolution_profile(graph, range_lo=range_lo, range_hi=range_hi, seed=seed)
    return [m for m in messages if "exhausted its" in m]


def test_profile_respects_its_declared_call_budget() -> None:
    """The budget constant must actually bound the solver calls it is named for."""
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    calls = _count_profile_calls(graph, range_lo=0.05, range_hi=0.95)
    assert calls <= _profile_call_budget(0.05, 0.95)
    assert calls > 20


def test_the_call_budget_is_derived_from_the_dyadic_grid_of_the_range() -> None:
    """The budget must be the grid size the two split brakes admit, not a flat number.

    Every evaluated γ is an endpoint or a midpoint of a halved interval, so all of
    them land on the dyadic grid of the initial span, and the tolerance caps how
    fine that grid gets at ``2**ceil(log2(span/tol)) + 1`` distinct points. A flat
    ceiling below that count truncates on graphs whose change-points are dense
    enough to demand the whole grid; a budget equal to it provably cannot.
    """
    for lo, hi in ((0.05, 0.95), (0.1, 0.2), (0.0001, 0.9999), (0.05, 0.06)):
        span = hi - lo
        levels = min(
            max(math.ceil(math.log2(span / _PROFILE_GAMMA_TOLERANCE)), 0), _PROFILE_MAX_DEPTH
        )
        assert _profile_call_budget(lo, hi) == min(2**levels + 1, _PROFILE_MAX_CALLS)

    # The default production range must resolve to a real grid bound, and one
    # strictly above the 240 flat ceiling that used to truncate above n=1000.
    assert _profile_call_budget(0.05, 0.95) == 257

    # A range narrow enough to need no splitting still admits its two endpoints.
    assert _profile_call_budget(0.05, 0.05 + _PROFILE_GAMMA_TOLERANCE) == 2

    # Wider ranges cost more, never less: a budget that ignores the range is flat.
    budgets = [_profile_call_budget(0.05, hi) for hi in (0.1, 0.3, 0.95, 5.0)]
    assert budgets == sorted(budgets)
    assert len(set(budgets)) > 1

    # The clamp is the outer brake and must never be exceeded.
    assert _profile_call_budget(0.001, 1e6) == _PROFILE_MAX_CALLS


def test_the_clamp_is_bracketed_between_the_default_grid_and_a_wall_clock_ceiling() -> None:
    """``_PROFILE_MAX_CALLS`` must be provably above the default grid and below a cost cap.

    It is only the outer brake now, so raising it changes nothing at the default
    range -- which means nothing at that range can pin it and the constant needs
    its own two-sided guard.

    Lower bound: it must not sit under the default range's 257-point grid, or the
    clamp would reintroduce exactly the truncation the derived budget removed.

    Upper bound: a truncating range spends the clamp in full, and a Leiden call
    on the largest graph the pipeline plausibly sees (n=8000, ~35k edges) measured
    0.177s. 512 calls is a 90s worst case; 10x that is 15 minutes, which is not a
    profile anyone waits for. Both directions have to fail here.
    """
    assert _profile_call_budget(0.05, 0.95) == 257
    assert _profile_call_budget(0.05, 0.95) < _PROFILE_MAX_CALLS

    seconds_per_call_at_the_largest_expected_graph = 0.177
    worst_case_seconds = _PROFILE_MAX_CALLS * seconds_per_call_at_the_largest_expected_graph
    assert worst_case_seconds <= 120.0, f"a truncating profile could run {worst_case_seconds:.0f}s"


def test_a_grid_fine_enough_to_hit_the_clamp_truncates_audibly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clamp is a real brake, not decoration: past it the profile must warn.

    Widening the γ range does not get there -- above roughly γ=1 every partition
    is a shatter, so equal endpoint counts prune those intervals unsplit and a
    range of [0.05, 200] still drains in 200 calls. Tightening the tolerance is
    the axis that does: at 1e-5 the grid is 131073 points, the clamp binds at 512
    calls with 128 intervals still pending, and that has to be audible.
    """
    monkeypatch.setattr(community_mod, "_PROFILE_GAMMA_TOLERANCE", 1e-5)
    assert _profile_call_budget(0.05, 0.95) == _PROFILE_MAX_CALLS

    graph, _truth = _planted_graph(n_per=30, n_blocks=4, seed=0)
    warnings = _truncation_warnings(graph, range_lo=0.05, range_hi=0.95)
    assert len(warnings) == 1
    assert "truncated" in warnings[0]
    assert "unsplit spanning" in warnings[0]


def test_the_budget_lets_the_queue_drain_on_a_graph_that_demands_the_whole_grid() -> None:
    """A graph dense in change-points must finish unsplit-free, not hit the ceiling.

    This is the guard the old flat 240 lacked. On this shape the bisection needs
    253 distinct γ; anything at or below 240 stops with 13 intervals still pending
    and drops real change-points, while the derived 257 drains the queue. Lowering
    the budget -- or reinstating a flat ceiling under the grid size -- fails here.
    """
    graph, _truth = _planted_graph(n_per=500, n_blocks=4, seed=0)
    budget = _profile_call_budget(0.05, 0.95)
    calls = _count_profile_calls(graph, range_lo=0.05, range_hi=0.95)

    # Saturation, the property the old guard's graph never reached: this shape
    # spends more than the retired 240 ceiling would have allowed.
    assert calls > 240, f"graph no longer saturates the retired ceiling: {calls} calls"
    assert calls <= budget

    # Draining is what the derived budget buys: the queue empties, so the
    # truncation warning stays silent and the extra change-points survive.
    profile = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
    assert not _truncation_warnings(graph, range_lo=0.05, range_hi=0.95)
    assert len(profile) >= 220, f"profile truncated to {len(profile)} rows"

    # The retired ceiling is exhibited alongside to show those rows are the ones
    # it dropped, so the row floor above is a real difference rather than a
    # number that any budget satisfies.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(community_mod, "_profile_call_budget", _fixed_budget(240))
        truncated = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
        assert _truncation_warnings(graph, range_lo=0.05, range_hi=0.95)
    assert len(truncated) < len(profile)


def test_raising_the_budget_past_the_grid_buys_nothing() -> None:
    """The derived budget is sufficient, so a larger one must change no output.

    Together with the saturation guard above this brackets the budget from both
    sides: below it the profile truncates, above it nothing improves. That is what
    makes 257 the right number rather than merely a large one.
    """
    graph, _truth = _planted_graph(n_per=500, n_blocks=4, seed=0)
    baseline = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(community_mod, "_PROFILE_MAX_CALLS", 10 * _PROFILE_MAX_CALLS)
        assert _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42) == baseline


def test_the_profile_stays_quiet_at_the_sizes_the_pipeline_actually_runs() -> None:
    """No production-scale shape may truncate; the warning is for misconfiguration.

    The retired flat ceiling was silent only up to n=1000 and truncated at every
    larger size, so "no swept shape truncates" has to be asserted on shapes that
    reach past that boundary rather than stopping at it.
    """
    for n_per, n_blocks in ((250, 4), (500, 4)):
        graph, _truth = _planted_graph(n_per=n_per, n_blocks=n_blocks, seed=0)
        warnings = _truncation_warnings(graph, range_lo=0.05, range_hi=0.95)
        assert warnings == [], f"n={n_per * n_blocks} truncated: {warnings}"


def test_profile_change_points_are_at_least_as_fine_as_the_tolerance() -> None:
    """Adjacent change-points must not be spaced coarser than the tolerance allows."""
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    profile = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(profile)]
    assert gaps
    assert min(gaps) > 0.0
    # Unresolved intervals are the only source of a gap wider than the tolerance,
    # and a complete profile leaves few of them.
    coarse = [g for g in gaps if g > 4 * _PROFILE_GAMMA_TOLERANCE]
    assert len(coarse) <= 0.25 * len(gaps)


def test_pick_zoom_returns_three_different_gammas_on_a_structured_graph() -> None:
    """coarse / medium / fine must name three scales, not one γ three times.

    Truncation manufactures a terminal plateau that dominates every real one, so
    ``medium`` collapses onto it and ``fine`` lands on the same γ.
    """
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    profile = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)

    picks = [_pick_zoom(profile, level, n_nodes=graph.n_nodes) for level in ZOOM_LEVELS]
    assert len(set(picks)) == 3, f"coarse/medium/fine collapsed onto {picks}"
    coarse, medium, fine = picks
    assert coarse < medium < fine

    counts = [len(set(_leiden_membership(graph, gamma=g, seed=42, n_iterations=-1))) for g in picks]
    assert counts[0] < counts[1] < counts[2], f"γ {picks} gave counts {counts}"


def test_pick_zoom_medium_lands_on_a_genuinely_flat_plateau() -> None:
    """``medium``'s plateau must hold its community count when sampled across it.

    The plateau a truncated profile invents is not flat at all: solving directly
    across it walks the count steadily upward.
    """
    graph, _truth = _planted_graph(n_per=50, n_blocks=4, seed=0)
    profile = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
    medium = _pick_zoom(profile, "medium", n_nodes=graph.n_nodes)

    row = next(row for row in profile if row[0] == medium)
    width = row[3] / 10000.0
    assert width > 0.0
    observed = [
        len(set(_leiden_membership(graph, gamma=medium + width * f, seed=42, n_iterations=-1)))
        for f in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    # Flat to within Leiden's own heuristic wobble, not merely trending.
    assert max(observed) - min(observed) <= 0.1 * row[1] + 2, observed


def test_pick_zoom_medium_skips_the_all_singletons_row() -> None:
    """A total shatter is stable over a wide γ span but is not a scale."""
    profile = [
        (0.10, 4, 9.0, 100),
        (0.40, 9, 3.0, 150),
        (0.80, 24, 0.0, 5000),
    ]
    assert _pick_zoom(profile, "medium", n_nodes=24) == pytest.approx(0.40)
    # With no node count to compare against, the longest plateau still wins.
    assert _pick_zoom(profile, "medium") == pytest.approx(0.80)


def test_pick_zoom_medium_skips_a_near_total_shatter_too() -> None:
    """159 communities over 160 nodes is a shatter even though it is not exactly N.

    A bound of ``n_comm < n_nodes`` admits it, and it owns the longest plateau, so
    ``medium`` lands on a partition with ARI-vs-truth near zero.
    """
    profile = [
        (0.10, 4, 40.0, 200),
        (0.45, 12, 12.0, 300),
        (0.75, 159, 0.01, 4000),
    ]
    assert _pick_zoom(profile, "medium", n_nodes=160) == pytest.approx(0.45)


def test_pick_zoom_medium_requires_mean_community_size_of_at_least_two() -> None:
    """The cutoff is ``n_nodes / 2`` exactly, so a row at the boundary is eligible."""
    profile = [(0.10, 3, 40.0, 100), (0.50, 50, 1.0, 900)]
    assert _pick_zoom(profile, "medium", n_nodes=100) == pytest.approx(0.50)
    # One community more and the same row is a shatter.
    profile = [(0.10, 3, 40.0, 100), (0.50, 51, 1.0, 900)]
    assert _pick_zoom(profile, "medium", n_nodes=100) == pytest.approx(0.10)


def test_pick_zoom_medium_falls_back_when_every_row_is_degenerate() -> None:
    profile = [(0.10, 1, 9.0, 100), (0.80, 6, 0.0, 500)]
    assert _pick_zoom(profile, "medium", n_nodes=6) == pytest.approx(0.80)


def test_profile_rejects_an_inverted_gamma_range() -> None:
    graph, _truth = _planted_graph()
    with pytest.raises(ValueError, match="empty γ range"):
        _compute_resolution_profile(graph, range_lo=0.9, range_hi=0.1, seed=42)


def test_profile_is_deterministic_across_repeated_calls() -> None:
    graph, _truth = _planted_graph()
    first = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
    second = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
    assert first == second


# ---------------------------------------------------------------------------
# γ guard and the iterations mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gamma", [0.0, -0.1, -1.0])
def test_leiden_rejects_non_positive_gamma_with_a_readable_message(gamma: float) -> None:
    graph, _truth = _planted_graph()
    with pytest.raises(ValueError, match="resolution must be strictly positive"):
        _leiden_membership(graph, gamma=gamma, seed=42, n_iterations=-1)


@pytest.mark.parametrize("n_iterations", [-1, 0, -99])
def test_non_positive_iterations_maps_to_the_fixed_cycle_count(n_iterations: int) -> None:
    """A negative count must not reach the solver, which rejects unsigned negatives."""
    graph, _truth = _planted_graph()
    mapped = _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=LEIDEN_CYCLES_FOR_UNBOUNDED)
    assert _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=n_iterations) == mapped


def test_positive_iterations_passes_through_unmapped() -> None:
    """An explicit 1 must stay 1, not silently become the unbounded stand-in."""
    graph, _truth = _planted_graph()
    one = _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=1)
    unbounded = _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=-1)
    assert LEIDEN_CYCLES_FOR_UNBOUNDED > 1
    assert _cpm_quality(graph, one, gamma=0.1) <= _cpm_quality(graph, unbounded, gamma=0.1)


def test_unbounded_cycle_count_is_enough_to_reach_the_quality_ceiling() -> None:
    """More cycles than the configured stand-in must not find a better partition."""
    graph, _truth = _planted_graph()
    for gamma in (0.05, 0.1, 0.3):
        base = _cpm_quality(
            graph,
            _leiden_membership(graph, gamma=gamma, seed=42, n_iterations=-1),
            gamma=gamma,
        )
        more = _cpm_quality(
            graph,
            _leiden_membership(graph, gamma=gamma, seed=42, n_iterations=12),
            gamma=gamma,
        )
        assert base >= more - 1e-9


# ---------------------------------------------------------------------------
# Isolated nodes
# ---------------------------------------------------------------------------


def test_isolated_nodes_get_distinct_singleton_communities() -> None:
    """Nodes absent from the solver's output must not share a leftover bucket."""
    graph = _build_graph(7, [(0, 1), (0, 2), (1, 2)], [1.0, 1.0, 1.0])
    labels = _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=-1)
    assert len(labels) == 7
    assert all(cid >= 0 for cid in labels)
    isolated = labels[3:]
    assert len(set(isolated)) == 4
    assert not set(isolated) & set(labels[:3])


def test_an_all_isolated_graph_is_all_singletons() -> None:
    graph = _build_graph(5, [], [])
    assert sorted(_leiden_membership(graph, gamma=0.5, seed=42, n_iterations=-1)) == [0, 1, 2, 3, 4]


def test_isolated_nodes_collapse_to_noise_individually() -> None:
    """Distinct singleton ids are what lets min_size sweep them all to noise."""
    graph = _build_graph(7, [(0, 1), (0, 2), (1, 2)], [1.0, 1.0, 1.0])
    labels = _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=-1)
    rows, n_real, n_noise = _relabel_and_collapse(
        labels,
        [f"s{i}" for i in range(7)],
        min_size=3,
        medoid_indices=set(),
        coherence={},
        gamma_used=0.1,
    )
    assert n_real == 1
    assert n_noise == 4
    assert len(rows) == 7


# ---------------------------------------------------------------------------
# scipy connectivity replacement
# ---------------------------------------------------------------------------


def test_adjacency_csr_is_symmetric_and_carries_the_weights() -> None:
    """``connected_components(directed=False)`` ignores asymmetry, so assert it here.

    Without this the symmetrization is invisible to every other test, and a
    future caller that does look at direction would silently read half a graph.
    """
    graph = _build_graph(4, [(0, 1), (1, 3)], [0.4, 0.9])
    dense = np.asarray(_adjacency_csr(graph).todense())
    assert np.array_equal(dense, dense.T)
    assert dense[0, 1] == pytest.approx(0.4)
    assert dense[1, 0] == pytest.approx(0.4)
    assert dense[1, 3] == pytest.approx(0.9)
    assert dense[3, 1] == pytest.approx(0.9)
    assert dense.sum() == pytest.approx(2 * (0.4 + 0.9))


def test_adjacency_csr_of_an_edgeless_graph_is_all_zero() -> None:
    dense = np.asarray(_adjacency_csr(_build_graph(3, [], [])).todense())
    assert dense.shape == (3, 3)
    assert not dense.any()


def test_warn_disconnected_fires_on_a_community_of_two_triangles(
    captured_logs: list[str],
) -> None:
    """One community spanning two disjoint triangles has 2 components."""
    graph = _build_graph(
        6,
        [(0, 1), (0, 2), (1, 2), (3, 4), (3, 5), (4, 5)],
        [1.0] * 6,
    )
    _warn_disconnected(graph, [0] * 6)
    assert len(captured_logs) == 1
    assert "2 weakly-connected components" in captured_logs[0]
    assert "sizes=[3, 3]" in captured_logs[0]


def test_warn_disconnected_stays_quiet_on_a_connected_community(
    captured_logs: list[str],
) -> None:
    graph = _build_graph(4, [(0, 1), (1, 2), (2, 3)], [1.0, 1.0, 1.0])
    _warn_disconnected(graph, [0, 0, 0, 0])
    assert captured_logs == []


def test_warn_disconnected_skips_communities_below_three_nodes(
    captured_logs: list[str],
) -> None:
    """A 2-node community with no edge between them is disconnected but ignored."""
    graph = _build_graph(4, [(0, 2), (1, 3)], [1.0, 1.0])
    _warn_disconnected(graph, [0, 0, 1, 1])
    assert captured_logs == []


def test_warn_disconnected_ignores_noise_labels(captured_logs: list[str]) -> None:
    graph = _build_graph(6, [(0, 1), (0, 2), (1, 2)], [1.0] * 3)
    _warn_disconnected(graph, [-1] * 6)
    assert captured_logs == []


def test_warn_disconnected_reports_component_sizes_descending(
    captured_logs: list[str],
) -> None:
    """A 4-node star plus a 2-node edge in one community: sizes [4, 2]."""
    graph = _build_graph(
        6,
        [(0, 1), (0, 2), (0, 3), (4, 5)],
        [1.0] * 4,
    )
    _warn_disconnected(graph, [0] * 6)
    assert len(captured_logs) == 1
    assert "sizes=[4, 2]" in captured_logs[0]


def test_connectivity_is_direction_agnostic_because_the_adjacency_is_symmetric(
    captured_logs: list[str],
) -> None:
    """The direction argument is inert precisely because the input is symmetrized.

    ``_adjacency_csr`` writes both ``(u, v)`` and ``(v, u)``, so weak and strong
    components coincide on every subgraph it can produce.  Over 3000 random
    graph/subset draws the two modes never disagreed, so this asserts the
    property that makes ``directed=False`` safe rather than pretending the flag
    itself is observable.
    """
    from scipy.sparse.csgraph import connected_components

    rng = np.random.default_rng(0)
    for _trial in range(150):
        n = int(rng.integers(3, 12))
        candidates = [(u, v) for u in range(n) for v in range(u + 1, n)]
        density = rng.uniform(0.05, 0.6)
        kept = [pair for pair in candidates if rng.random() < density]
        graph = _build_graph(n, kept, [float(rng.uniform(0.1, 1.0)) for _ in kept])
        adjacency = _adjacency_csr(graph)

        size = int(rng.integers(1, n + 1))
        chosen = np.asarray(sorted(rng.choice(n, size=size, replace=False)), dtype=np.int32)
        sub = adjacency[chosen][:, chosen]

        n_weak, weak_labels = connected_components(sub, directed=False)
        n_strong, strong_labels = connected_components(sub, directed=True, connection="strong")
        assert n_weak == n_strong
        assert sorted(np.bincount(weak_labels).tolist()) == sorted(
            np.bincount(strong_labels).tolist()
        )


def test_warn_disconnected_counts_each_bad_community_separately(
    captured_logs: list[str],
) -> None:
    """Two split communities produce two warnings, one per community."""
    graph = _build_graph(
        12,
        [(0, 1), (0, 2), (3, 4), (3, 5), (6, 7), (6, 8), (9, 10), (9, 11)],
        [1.0] * 8,
    )
    _warn_disconnected(graph, [0] * 6 + [1] * 6)
    assert len(captured_logs) == 2


# ---------------------------------------------------------------------------
# Medoid centrality
# ---------------------------------------------------------------------------


def test_medoid_is_the_most_central_member_not_the_least() -> None:
    """A hub-and-spokes community must elect the hub.

    Counting one medoid per community is satisfied by picking the *least*
    central member just as well as the most, so the choice needs its own guard.
    """
    sim = np.array(
        [
            [0.0, 0.9, 0.9, 0.9],
            [0.9, 0.0, 0.2, 0.2],
            [0.9, 0.2, 0.0, 0.2],
            [0.9, 0.2, 0.2, 0.0],
        ]
    )
    medoids, coherence = _compute_medoid_and_coherence(sim, [0, 0, 0, 0])
    assert medoids == {0}
    assert coherence[0] == pytest.approx((0.9 * 3 + 0.2 * 3) / 6)


def test_medoid_maximizes_mean_similarity_to_other_members() -> None:
    """Cross-check against the definition on random communities."""
    rng = np.random.default_rng(5)
    for _trial in range(40):
        n = int(rng.integers(3, 9))
        raw = rng.uniform(0.0, 1.0, size=(n, n))
        sim = (raw + raw.T) / 2.0
        np.fill_diagonal(sim, 0.0)
        medoids, _coherence = _compute_medoid_and_coherence(sim, [0] * n)
        mean_to_others = (sim.sum(axis=1) - np.diag(sim)) / (n - 1)
        assert medoids == {int(np.argmax(mean_to_others))}


def test_medoid_is_per_community_not_global() -> None:
    """Two communities each elect their own hub."""
    sim = np.array(
        [
            [0.0, 0.9, 0.9, 0.1, 0.1, 0.1],
            [0.9, 0.0, 0.2, 0.1, 0.1, 0.1],
            [0.9, 0.2, 0.0, 0.1, 0.1, 0.1],
            [0.1, 0.1, 0.1, 0.0, 0.2, 0.2],
            [0.1, 0.1, 0.1, 0.2, 0.0, 0.8],
            [0.1, 0.1, 0.1, 0.2, 0.8, 0.0],
        ]
    )
    medoids, _coherence = _compute_medoid_and_coherence(sim, [0, 0, 0, 1, 1, 1])
    assert medoids == {0, 4}


# ---------------------------------------------------------------------------
# Edge-ordering property that keeps the solver deterministic
# ---------------------------------------------------------------------------


def test_mutual_knn_emits_canonical_sorted_upper_triangle_order() -> None:
    """Leiden is edge-order sensitive, so this ordering is part of the contract."""
    sim, _truth = _planted_similarity()
    edges, weights = _build_mutual_knn(sim, k=15, floor=0.3)
    assert edges
    assert len(edges) == len(weights)
    assert all(u < v for u, v in edges)
    assert edges == sorted(edges)
    assert len(set(edges)) == len(edges)


def test_shuffling_the_edge_list_changes_the_partition() -> None:
    """Documents WHY the ordering assertion above matters, by exhibiting the risk."""
    graph, _truth = _planted_graph()
    canonical = _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=-1)
    order = np.random.default_rng(7).permutation(len(graph.edges))
    shuffled_graph = _build_graph(
        graph.n_nodes,
        [graph.edges[i] for i in order],
        [graph.weights[i] for i in order],
    )
    shuffled = _leiden_membership(shuffled_graph, gamma=0.1, seed=42, n_iterations=-1)
    assert shuffled != canonical


def test_the_seam_hands_the_solver_the_edge_list_in_graph_order() -> None:
    """The seam must not reorder edges on their way to the solver.

    Every assertion that compares the seam against itself survives the seam
    reversing, shuffling or rotating the list internally, because both sides of
    the comparison move together.  Calling the solver directly with the graph's
    own order is the independent oracle that pins it, and the reversed call is
    exhibited alongside to show the oracle discriminates.
    """
    import graspologic_native as gn

    graph, _truth = _planted_graph()
    native = [
        (str(u), str(v), float(w)) for (u, v), w in zip(graph.edges, graph.weights, strict=True)
    ]

    def solve(edge_list: list[tuple[str, str, float]]) -> list[int]:
        # graspologic_native is a native extension that ships neither stubs nor a
        # `.pyi`, so no checker can see `leiden` on the module.
        _quality, partition = gn.leiden(  # ty: ignore[unresolved-attribute] # pyright: ignore[reportAttributeAccessIssue]
            edges=edge_list,
            resolution=0.1,
            use_modularity=False,
            seed=42,
            iterations=LEIDEN_CYCLES_FOR_UNBOUNDED,
        )
        return [int(partition[str(i)]) for i in range(graph.n_nodes)]

    expected = solve(native)
    assert _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=-1) == expected
    assert solve(native[::-1]) != expected


def test_build_graph_rejects_mismatched_edge_and_weight_lengths() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        _build_graph(3, [(0, 1), (1, 2)], [1.0])


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_seed_gives_an_identical_partition_in_process() -> None:
    graph, _truth = _planted_graph()
    first = _run_leiden_cpm(graph, gamma=0.1, seed=42, n_iterations=-1)
    second = _run_leiden_cpm(graph, gamma=0.1, seed=42, n_iterations=-1)
    assert first == second


def test_same_seed_gives_an_identical_partition_across_processes() -> None:
    """A fresh interpreter must reproduce the membership byte for byte."""
    graph, _truth = _planted_graph()
    expected = _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=-1)

    script = textwrap.dedent(
        """
        import json
        import sys
        sys.path.insert(0, sys.argv[1])
        from test_community_backend import _planted_graph
        from atif_analytics.domain.structure.community import _leiden_membership

        graph, _ = _planted_graph()
        labels = _leiden_membership(graph, gamma=0.1, seed=42, n_iterations=-1)
        print(json.dumps(labels))
        """
    )
    proc = subprocess.run(  # noqa: S603 — argv is this interpreter plus a literal script
        [sys.executable, "-c", script, str(__file__.rsplit("/", 1)[0])],
        capture_output=True,
        text=True,
        check=True,
    )
    import json

    assert json.loads(proc.stdout.strip().splitlines()[-1]) == expected


def test_a_different_seed_is_allowed_to_differ_but_stays_self_consistent() -> None:
    graph, _truth = _planted_graph()
    labels, quality = _run_leiden_cpm(graph, gamma=0.1, seed=1234, n_iterations=-1)
    assert quality == pytest.approx(_cpm_quality(graph, labels, gamma=0.1))
    assert len(labels) == graph.n_nodes


def test_the_caller_seed_reaches_the_solver_rather_than_a_constant() -> None:
    """Distinct seeds must give distinct partitions, and one seed must repeat.

    Asserting only "same seed reproduces" is satisfied by a hard-coded constant
    seed just as well as by the caller's, so the differing half is what makes a
    substituted seed value visible at all.
    """
    graph, _truth = _planted_graph()
    by_seed = {
        seed: _leiden_membership(graph, gamma=0.1, seed=seed, n_iterations=-1)
        for seed in (0, 1, 42, 999, 12345)
    }
    distinct = {tuple(labels) for labels in by_seed.values()}
    assert len(distinct) == len(by_seed), "seeds collapsed onto one partition"

    for seed, labels in by_seed.items():
        assert _leiden_membership(graph, gamma=0.1, seed=seed, n_iterations=-1) == labels


def test_the_profile_seed_reaches_the_solver_rather_than_a_constant() -> None:
    """The bisection's own Leiden calls must key on the caller's seed too."""
    graph, _truth = _planted_graph()
    profiles = {
        seed: _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=seed)
        for seed in (0, 42, 12345)
    }
    assert len({tuple(rows) for rows in profiles.values()}) == len(profiles)
    for seed, rows in profiles.items():
        assert _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=seed) == rows


# ---------------------------------------------------------------------------
# Planted-structure recovery (sanity, not exactness)
# ---------------------------------------------------------------------------


def _weighted_purity(labels: list[int], truth: list[int]) -> float:
    """Fraction of nodes sitting in their community's majority planted block.

    Purity, not ARI: Leiden may legitimately split one planted block into
    several pure sub-communities, which craters ARI while leaving recovery
    intact.  Purity only punishes actual block mixing.
    """
    by_community: dict[int, list[int]] = {}
    for node_idx, cid in enumerate(labels):
        by_community.setdefault(cid, []).append(truth[node_idx])
    hits = sum(max(members.count(b) for b in set(members)) for members in by_community.values())
    return hits / len(labels)


def test_planted_blocks_are_recovered_at_the_coarse_end() -> None:
    """Communities must line up with the planted blocks, not cut across them."""
    graph, truth = _planted_graph(n_per=40, n_blocks=3, seed=11, spread=0.25)
    labels, _quality = _run_leiden_cpm(graph, gamma=0.05, seed=42, n_iterations=-1)
    assert _weighted_purity(labels, truth) >= 0.9

    # Each block's largest community must hold most of that block, so the
    # blocks are recovered rather than shredded into many tiny pure pieces.
    sizes: dict[tuple[int, int], int] = {}
    for node_idx, cid in enumerate(labels):
        key = (truth[node_idx], cid)
        sizes[key] = sizes.get(key, 0) + 1
    for block in range(3):
        biggest = max(v for (b, _c), v in sizes.items() if b == block)
        assert biggest >= 0.5 * 40


def test_planted_recovery_survives_a_change_of_seed() -> None:
    """Recovery is a property of the graph, not of one lucky seed."""
    graph, truth = _planted_graph(n_per=40, n_blocks=3, seed=11, spread=0.25)
    for seed in (1, 7, 42, 99):
        labels, _quality = _run_leiden_cpm(graph, gamma=0.05, seed=seed, n_iterations=-1)
        assert _weighted_purity(labels, truth) >= 0.9


def test_purity_collapses_on_a_deliberately_wrong_partition() -> None:
    """Proves the purity threshold discriminates: a round-robin labeling fails it."""
    graph, truth = _planted_graph(n_per=40, n_blocks=3, seed=11, spread=0.25)
    scrambled = [i % 3 for i in range(graph.n_nodes)]
    assert _weighted_purity(scrambled, truth) < 0.5


def test_the_medium_pick_is_not_a_near_total_shatter(
    captured_logs: list[str],
) -> None:
    """The production default must return communities, not one node each.

    Purity cannot see this: a partition of 159 singletons over 160 nodes scores
    purity 1.0 while recovering nothing, which is why the mean community size is
    asserted here instead.
    """
    for n_per, n_blocks, seed in ((40, 4, 0), (40, 4, 11), (50, 4, 11), (20, 6, 0)):
        graph, truth = _planted_graph(n_per=n_per, n_blocks=n_blocks, seed=seed, spread=0.35)
        profile = _compute_resolution_profile(graph, range_lo=0.05, range_hi=0.95, seed=42)
        gamma = _pick_zoom(profile, "medium", n_nodes=graph.n_nodes)
        labels = _leiden_membership(graph, gamma=gamma, seed=42, n_iterations=-1)

        n_comm = len(set(labels))
        assert n_comm >= 2
        assert n_comm <= graph.n_nodes / 2, (
            f"medium γ={gamma:.4f} gave {n_comm} communities over {graph.n_nodes} nodes"
        )
        assert _weighted_purity(labels, truth) >= 0.8


def test_fine_gamma_shatters_more_than_coarse_gamma() -> None:
    graph, _truth = _planted_graph()
    coarse, _ = _run_leiden_cpm(graph, gamma=0.05, seed=42, n_iterations=-1)
    fine, _ = _run_leiden_cpm(graph, gamma=0.6, seed=42, n_iterations=-1)
    assert len(set(fine)) > len(set(coarse))
