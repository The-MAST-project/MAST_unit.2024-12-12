"""The source selection (Stage 0 of the source-filtering plan): what it measures and what it rejects.

Synthetic frames only: Gaussian stars on a noisy sky, with one star planted for each reject and a
fold-mirror shadow built directly as a `ShadowModel`. No solver, no unit.
"""

from __future__ import annotations

import numpy as np
import pytest

from imaging.mirror_shadow import ShadowModel
from solvers.source_selection import (
    FWHM_PER_SIGMA,
    KEPT,
    SPARE,
    SelectionParams,
    select_sources,
    window_centroid,
)

NX, NY = 1200, 900
CX = (NX - 1) / 2.0
SIGMA = 3.2  # star sigma, pixels: FWHM 7.5
SKY, NOISE = 500.0, 6.0
PARAMS = SelectionParams(n_target=10, min_snr=25.0)


def _star(frame: np.ndarray, x: float, y: float, flux: float) -> None:
    half = 20
    x0, y0 = int(round(x)), int(round(y))
    ys, xs = np.mgrid[
        max(0, y0 - half) : min(frame.shape[0], y0 + half + 1), max(0, x0 - half) : min(frame.shape[1], x0 + half + 1)
    ]
    frame[ys, xs] += flux / (2 * np.pi * SIGMA**2) * np.exp(-((xs - x) ** 2 + (ys - y) ** 2) / (2 * SIGMA**2))


def _shadow() -> ShadowModel:
    """A vertical band through the frame's centre column: 50% deficit within 40 px, none beyond 120 px."""
    s = np.arange(-600.0, 601.0, 16.0)
    profile = np.clip(0.5 * (120.0 - np.abs(s)) / 80.0, 0.0, 0.5)
    profile[np.abs(s) < 40] = 0.5
    return ShadowModel(
        present=True,
        image_shape=(NY, NX),
        angle=np.pi / 2,
        offset=0.0,
        umbra_half_width=40.0,
        penumbra_half_width=120.0,
        depth=0.5,
        prominence=20.0,
        s=s,
        profile=profile,
    )


#: planted stars: name -> (x, y, flux)
PLANTED = {
    "in_umbra": (CX + 10, 300, 9000),
    "in_penumbra": (CX + 100, 500, 9000),  # deficit 0.125
    "at_the_penumbra_edge": (CX + 118, 650, 9000),  # deficit 0.0125: passes the 5% cut
    "saturated": (250, 200, 3.5e6),
    "close_pair_a": (900, 150, 8000),  # 18 px apart: two detections, each a blend of the other
    "close_pair_b": (918, 150, 8000),
    "merged_pair_a": (900, 700, 8000),  # 14 px apart: one broad detection
    "merged_pair_b": (914, 700, 8000),
    "at_the_edge": (6, 450, 8000),
    "faint": (1000, 450, 1500),
}


def _frame(*, planted=True, n_clean=24, seed=11) -> tuple[np.ndarray, dict[str, tuple[float, float]]]:
    rng = np.random.default_rng(seed)
    frame = rng.normal(SKY, NOISE, (NY, NX))
    placed: list[tuple[float, float]] = []
    positions: dict[str, tuple[float, float]] = {}
    if planted:
        for name, (x, y, flux) in PLANTED.items():
            _star(frame, x, y, flux)
            placed.append((x, y))
            positions[name] = (x, y)
        # a hot pixel group: 2 x 3 sharp pixels, area 6
        frame[750:752, 150:153] += 3000.0
        placed.append((151.0, 750.5))
        positions["hot_pixel"] = (151.0, 750.5)
    clean = 0
    while clean < n_clean:
        x, y = rng.uniform(60, NX - 60), rng.uniform(60, NY - 60)
        if abs(x - CX) < 140 or any(np.hypot(x - px, y - py) < 70 for px, py in placed):
            continue
        _star(frame, x, y, rng.uniform(4000, 20000))
        placed.append((x, y))
        positions[f"clean_{clean}"] = (x, y)
        clean += 1
    return np.clip(frame, 0, 65535).astype(np.uint16), positions


@pytest.fixture(scope="module")
def scene():
    frame, positions = _frame()
    return frame, positions, select_sources(frame, shadow=_shadow(), params=PARAMS)


def _fate_at(selection, position, tolerance=3.0):
    distance = np.hypot(selection.x - position[0], selection.y - position[1])
    i = int(np.argmin(distance))
    assert distance[i] < tolerance, f"nothing detected near {position}"
    return selection.fate[i]


# ----- the window centroid ---------------------------------------------------------------------


def test_the_window_centroid_finds_a_star_to_a_fiftieth_of_a_pixel():
    data = np.zeros((90, 100))
    _star(data, 50.37, 40.81, 10000.0)

    x, y, mass, sum_w2, converged = window_centroid(data, 49.0, 42.0, sigma_w=SIGMA)

    assert converged
    assert (x, y) == pytest.approx((50.37, 40.81), abs=0.02)
    assert mass > 0 and sum_w2 > 0


def test_a_window_centroid_walking_off_its_stamp_is_reported_not_trusted():
    data = np.zeros((90, 100))
    _star(data, 80.0, 40.0, 10000.0)

    x, y, _mass, _sum_w2, converged = window_centroid(data, 30.0, 40.0, sigma_w=SIGMA)

    assert not converged or not np.isfinite(x)


# ----- the rejects -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("in_umbra", "shadow"),
        ("in_penumbra", "shadow"),
        ("saturated", "saturated"),
        ("hot_pixel", "hot_pixel"),
        ("close_pair_a", "blended"),
        ("close_pair_b", "blended"),
        ("at_the_edge", "truncated"),
        ("faint", "faint"),
    ],
)
def test_each_planted_source_is_rejected_for_its_reason(scene, name, reason):
    _frame_, positions, selection = scene

    assert _fate_at(selection, positions[name]) == reason


def test_a_merged_pair_is_one_broad_source_and_is_rejected_as_blended(scene):
    _frame_, positions, selection = scene
    pair = (np.mean([positions["merged_pair_a"][0], positions["merged_pair_b"][0]]), 700.0)

    assert _fate_at(selection, pair, tolerance=8.0) == "blended"


def test_a_source_at_the_penumbra_edge_passes_the_five_percent_cut(scene):
    _frame_, positions, selection = scene

    assert _fate_at(selection, positions["at_the_penumbra_edge"]) in (KEPT, SPARE)


def test_clean_stars_are_never_rejected(scene):
    _frame_, positions, selection = scene
    clean = [p for name, p in positions.items() if name.startswith("clean_")]

    assert [(_fate_at(selection, p)) for p in clean if _fate_at(selection, p) not in (KEPT, SPARE)] == []


def test_every_failed_test_is_recorded_not_only_the_first(scene):
    _frame_, positions, selection = scene
    x, y = positions["saturated"]
    i = int(np.argmin(np.hypot(selection.x - x, selection.y - y)))

    assert selection.fate[i] == "saturated" and "saturated" in selection.all_reasons[i].split("|")


# ----- the frame as a whole --------------------------------------------------------------------


def test_the_seeing_is_measured_from_the_stars(scene):
    _frame_, _positions, selection = scene

    assert selection.fwhm_seeing_px == pytest.approx(7.5, abs=1.2)
    assert selection.sigma_window_px == pytest.approx(selection.fwhm_seeing_px / FWHM_PER_SIGMA, rel=1e-9)


def test_the_selection_is_deterministic(scene):
    frame, _positions, first = scene

    again = select_sources(frame, shadow=_shadow(), params=PARAMS)

    for name in ("x", "y", "flux", "snr", "fate", "sigma"):
        assert np.array_equal(getattr(first, name), getattr(again, name)), name


def test_the_kept_sources_reach_the_target_and_are_listed_brightest_first(scene):
    _frame_, _positions, selection = scene
    xylist = selection.xylist()

    assert selection.counts()["kept"] >= PARAMS.n_target
    assert xylist.shape == (selection.counts()["kept"], 3)
    assert np.all(np.diff(xylist[:, 2]) <= 0)
    assert selection.fit_order in (1, 2)


def test_without_a_shadow_nothing_is_rejected_for_one():
    frame, positions = _frame()

    selection = select_sources(frame, shadow=None, params=PARAMS)

    assert selection.counts()["shadow"] == 0
    assert _fate_at(selection, positions["in_umbra"]) in (KEPT, SPARE)


def test_a_shadow_that_was_not_found_rejects_nothing():
    frame, positions = _frame()
    absent = ShadowModel(present=False, image_shape=(NY, NX))

    assert select_sources(frame, shadow=absent, params=PARAMS).counts()["shadow"] == 0


def test_the_exclusion_width_replaces_the_deficit_test():
    """Swept by the survey: a plain distance from the band's centreline, whatever the depth there."""
    frame, positions = _frame()

    narrow = select_sources(frame, shadow=_shadow(), params=PARAMS, exclusion_half_width=50.0)
    wide = select_sources(frame, shadow=_shadow(), params=PARAMS, exclusion_half_width=200.0)

    assert _fate_at(narrow, positions["in_penumbra"]) in (KEPT, SPARE)  # 100 px out, beyond 50
    assert _fate_at(wide, positions["in_penumbra"]) == "shadow"
    assert _fate_at(wide, positions["at_the_penumbra_edge"]) == "shadow"  # passes the deficit test, not the width
    assert wide.counts()["shadow"] > narrow.counts()["shadow"]


# ----- how many, and the fit order -------------------------------------------------------------


@pytest.mark.parametrize(
    ("n_clean", "status", "order"),
    [(5, "too_few_sources", None), (12, "ok", 1), (30, "ok", 2)],
    ids=["five", "twelve", "thirty"],
)
def test_the_fit_order_follows_how_many_were_kept(n_clean, status, order):
    frame, _ = _frame(planted=False, n_clean=n_clean, seed=3)
    params = SelectionParams(n_target=n_clean, min_snr=25.0)

    selection = select_sources(frame, params=params)

    assert selection.status == status
    assert selection.fit_order == order


def test_a_grid_quota_stops_one_corner_from_supplying_every_star():
    rng = np.random.default_rng(5)
    frame = rng.normal(SKY, NOISE, (NY, NX))
    placed: list[tuple[float, float]] = []
    while len(placed) < 20:  # twenty stars, all in the top-left cell (300 x 300)
        x, y = rng.uniform(60, 280), rng.uniform(60, 280)
        if all(np.hypot(x - px, y - py) > 40 for px, py in placed):
            _star(frame, x, y, 9000.0)
            placed.append((x, y))
    frame = np.clip(frame, 0, 65535).astype(np.uint16)
    params = SelectionParams(n_target=30, n_max=24, min_snr=25.0)  # two per cell

    selection = select_sources(frame, params=params)

    assert selection.counts()["kept"] == 2
    assert selection.counts()["spare"] == 18


def test_the_radius_is_widened_until_there_are_enough_stars():
    frame, _ = _frame(planted=False, n_clean=24, seed=9)
    small = SelectionParams(n_target=18, min_snr=25.0)

    selection = select_sources(frame, params=small, low_coma_radius=60.0)

    assert selection.radius_used_px > 60.0
    assert selection.counts()["kept"] >= 18


def test_a_frame_with_no_stars_keeps_nothing():
    """Noise alone still gives a few spurious blobs at 2 sigma; none of them is kept."""
    frame = np.random.default_rng(1).normal(SKY, NOISE, (300, 400)).astype(np.float32)

    selection = select_sources(frame, params=PARAMS)

    assert selection.status in ("no_sources", "too_few_sources")
    assert selection.counts()["kept"] == 0 and selection.fit_order is None


def test_saturation_is_not_tested_on_a_float_frame_without_a_full_scale():
    frame, positions = _frame()

    selection = select_sources(frame.astype(np.float32), shadow=_shadow(), params=PARAMS)

    assert not selection.saturation_tested
    assert selection.counts()["saturated"] == 0


def test_a_full_scale_can_be_given_for_a_float_frame():
    frame, positions = _frame()
    params = SelectionParams(n_target=10, min_snr=25.0, full_scale=65535.0)

    selection = select_sources(frame.astype(np.float32), shadow=_shadow(), params=params)

    assert selection.saturation_tested
    assert _fate_at(selection, positions["saturated"]) == "saturated"
