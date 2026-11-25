"""The selection's window: its geometry (no images), and the windowed selection against the whole-frame one.

Synthetic frames only, built as in `test_source_selection.py`.
"""

from __future__ import annotations

import numpy as np
import pytest

from imaging.mirror_shadow import ShadowModel
from solvers.selection_window import (
    GridMask,
    box_area,
    plan_tiles,
    square_box,
    window_schedule,
)
from solvers.source_selection import SelectionParams, select_sources, shadow_allowed_mask

# ---------------------------------------------------------------------------------------- the geometry

SHAPE = (1000, 2000)  # (ny, nx)
CENTRE = (1000.0, 500.0)


def _band_mask(lo: float, hi: float, cell: int = 16) -> GridMask:
    """Allowed everywhere except the vertical strip lo <= x < hi."""
    mask = GridMask.open(SHAPE, cell)
    cells = mask.cells.copy()
    cells[:, lo // cell : -(-hi // cell)] = False
    return GridMask(cells, cell, SHAPE)


def test_without_a_shadow_the_windows_are_the_selections_own_radii():
    windows = window_schedule(GridMask.open(SHAPE), CENTRE, 100.0, 1.25)

    for k, window in enumerate(windows[:-1]):
        assert window.half_size == pytest.approx(100.0 * 1.25**k)
        assert window.box == square_box(CENTRE, 100.0 * 1.25**k, SHAPE, 16)
    assert windows[-1].exhausted and windows[-1].box == (0, 0, 2000, 1000)
    assert not any(w.exhausted for w in windows[:-1])


def test_windows_only_grow():
    windows = window_schedule(_band_mask(700, 1300), CENTRE, 100.0, 1.25)

    boxes = [w.box for w in windows]
    assert len(set(boxes)) == len(boxes)
    for a, b in zip(boxes, boxes[1:], strict=False):
        assert b[0] <= a[0] and b[1] <= a[1] and b[2] >= a[2] and b[3] >= a[3]


def test_a_shadow_over_the_centre_displaces_the_window_outward_to_the_same_allowed_area():
    mask = _band_mask(700, 1300)  # the plain 100 px window is wholly inside it
    plain = window_schedule(GridMask.open(SHAPE), CENTRE, 100.0, 1.25)
    shadowed = window_schedule(mask, CENTRE, 100.0, 1.25)

    first = shadowed[0]
    assert first.half_size > 300  # out to the band's edge and past it
    assert mask.area(first.box) >= box_area(plain[0].box) - 1e-9
    assert mask.area(first.box) < box_area(plain[0].box) + 600 * 16 * 2  # not much more than asked for


def test_the_schedule_ends_when_the_allowed_area_is_all_inside():
    mask = _band_mask(700, 1300)
    windows = window_schedule(mask, CENTRE, 100.0, 1.25)

    assert windows[-1].exhausted
    assert windows[-1].allowed_area == pytest.approx(mask.total_area)
    assert all(not w.exhausted for w in windows[:-1])


def test_tiles_cover_the_allowed_pieces_only_and_are_aligned():
    mask = _band_mask(700, 1300)
    rect = (0, 0, 2000, 1000)

    tiles = plan_tiles(rect, mask, pad=100, align=50)

    assert len(tiles) == 2  # the two flanks
    for tile in tiles:
        x0, y0, x1, y1 = tile.box
        assert x0 % 50 == 0 and y0 % 50 == 0
        assert (x1 % 50 == 0 or x1 == 2000) and (y1 % 50 == 0 or y1 == 1000)
        assert x0 <= max(0, tile.core[0] - 100 + 49) and x1 >= min(2000, tile.core[2] + 100 - 49)
        assert not (tile.core[0] < 700 < tile.core[2]) and not (tile.core[0] < 1300 < tile.core[2])


def test_a_piece_with_nothing_allowed_has_no_tile():
    assert plan_tiles((720, 0, 1280, 1000), _band_mask(700, 1300), pad=100, align=50) == []


def test_the_growth_must_exceed_one():
    with pytest.raises(ValueError, match="exceed 1"):
        window_schedule(GridMask.open(SHAPE), CENTRE, 100.0, 1.0)


# ------------------------------------------------------------------------- the windowed selection

NY, NX = 1500, 2400
SIGMA = 3.2
UNCAPPED = {"reach_factor": None, "reach_factor_closed_shadow": None}  # the equivalence tests compare with the whole frame
PARAMS = SelectionParams(seeing_scale=1.0, n_target=12, min_snr=20.0, box_size=50, **UNCAPPED)
CENTER = (1200.0, 750.0)
LOW_COMA = 300.0


def _stars(shadow_band: tuple[float, float] | None = None, n: int = 120, seed: int = 5):
    rng = np.random.default_rng(seed)
    frame = rng.normal(500.0, 6.0, (NY, NX))
    placed: list[tuple[float, float]] = []
    while len(placed) < n:
        x, y = rng.uniform(60, NX - 60), rng.uniform(60, NY - 60)
        if shadow_band is not None and shadow_band[0] - 150 < x < shadow_band[1] + 150 and False:
            continue
        if all(np.hypot(x - a, y - b) > 40 for a, b in placed):
            placed.append((x, y))
    for x, y in placed:
        flux = rng.uniform(4000, 20000)
        x0, y0 = int(round(x)), int(round(y))
        ys, xs = np.mgrid[max(0, y0 - 20) : min(NY, y0 + 21), max(0, x0 - 20) : min(NX, x0 + 21)]
        frame[ys, xs] += flux / (2 * np.pi * SIGMA**2) * np.exp(-((xs - x) ** 2 + (ys - y) ** 2) / (2 * SIGMA**2))
    return frame, placed


def _vertical_shadow(centre_x: float, half_width: float) -> ShadowModel:
    s = np.arange(-1600.0, 1601.0, 16.0)
    profile = np.where(np.abs(s) < half_width, 0.5, 0.0)
    return ShadowModel(
        present=True,
        image_shape=(NY, NX),
        angle=np.pi / 2,
        offset=centre_x - (NX - 1) / 2.0,
        umbra_half_width=half_width * 0.6,
        penumbra_half_width=half_width,
        depth=0.5,
        prominence=20.0,
        s=s,
        profile=profile,
    )


def _compare(whole, windowed, *, position_tolerance=0.05):
    """The windowed selection must have chosen the whole-frame stars, at the same places."""
    kept_whole = {(round(x, 1), round(y, 1)) for x, y, k in zip(whole.x, whole.y, whole.kept, strict=True) if k}
    kept_windowed = {(round(x, 1), round(y, 1)) for x, y, k in zip(windowed.x, windowed.y, windowed.kept, strict=True) if k}
    assert len(kept_whole) == int(whole.kept.sum()) and len(kept_windowed) == int(windowed.kept.sum())
    return kept_whole, kept_windowed


def test_without_a_shadow_and_with_enough_stars_it_stops_in_the_first_window_and_chooses_the_same_stars():
    frame, _ = _stars()
    whole = select_sources(frame, optical_center=CENTER, low_coma_radius=LOW_COMA * 2, params=PARAMS, windowed=False)
    windowed = select_sources(frame, optical_center=CENTER, low_coma_radius=LOW_COMA * 2, params=PARAMS, windowed=True)

    assert windowed.windows_used == 1
    assert windowed.area_measured_px < 0.7 * NX * NY
    assert windowed.radius_used_px == whole.radius_used_px == LOW_COMA * 2
    a, b = _compare(whole, windowed)
    assert a == b and len(a) >= PARAMS.n_target
    assert windowed.fwhm_seeing_px == pytest.approx(whole.fwhm_seeing_px, abs=0.3)


def test_the_window_grows_until_there_are_enough_stars_and_chooses_what_the_whole_frame_does():
    frame, _ = _stars(n=60)
    params = SelectionParams(seeing_scale=1.0, n_target=30, min_snr=20.0, box_size=50, **UNCAPPED)
    whole = select_sources(frame, optical_center=CENTER, low_coma_radius=150.0, params=params, windowed=False)
    windowed = select_sources(frame, optical_center=CENTER, low_coma_radius=150.0, params=params, windowed=True)

    assert windowed.windows_used > 1
    assert windowed.radius_used_px == pytest.approx(whole.radius_used_px)
    a, b = _compare(whole, windowed)
    assert a == b
    assert windowed.area_measured_px < 2.0 * NX * NY  # starved, it costs more than one whole-frame pass, but not much
    assert windowed.window_box[2] - windowed.window_box[0] > 600


def test_a_window_that_cannot_find_enough_ends_as_the_whole_frame_and_relaxes_like_it():
    frame, _ = _stars(n=14)
    params = SelectionParams(seeing_scale=1.0, n_target=30, min_snr=40.0, box_size=50, **UNCAPPED)
    whole = select_sources(frame, optical_center=CENTER, low_coma_radius=150.0, params=params, windowed=False)
    windowed = select_sources(frame, optical_center=CENTER, low_coma_radius=150.0, params=params, windowed=True)

    assert windowed.window_box == (0, 0, NX, NY)
    assert windowed.snr_relaxed == whole.snr_relaxed
    a, b = _compare(whole, windowed)
    assert a == b


def test_with_a_shadow_over_the_centre_nothing_is_detected_in_the_band_and_the_same_stars_are_chosen():
    frame, placed = _stars(n=150)
    shadow = _vertical_shadow(CENTER[0], 450.0)
    params = SelectionParams(seeing_scale=1.0, n_target=20, min_snr=20.0, box_size=50, **UNCAPPED)
    whole = select_sources(frame, shadow=shadow, optical_center=CENTER, low_coma_radius=300.0, params=params, windowed=False)
    windowed = select_sources(
        frame, shadow=shadow, optical_center=CENTER, low_coma_radius=300.0, params=params, windowed=True
    )

    in_band = np.abs(windowed.x - CENTER[0]) < 400
    assert not in_band.any()  # no source of the band was detected (and so none was spent measuring)
    assert windowed.area_measured_px < 1.6 * NX * NY
    a, b = _compare(whole, windowed)
    assert a == b and len(a) >= params.n_target


def test_the_allowed_mask_holds_every_cell_a_star_could_pass_in():
    shadow = _vertical_shadow(CENTER[0], 450.0)
    mask = shadow_allowed_mask(shadow, (NY, NX), 0.05, cell=16)

    assert not mask.cells[:, int((CENTER[0] - 400) // 16) : int((CENTER[0] + 400) // 16)].any()
    assert mask.cells[:, : int((CENTER[0] - 500) // 16)].all() and mask.cells[:, int((CENTER[0] + 500) // 16) + 1 :].all()
    assert shadow_allowed_mask(None, (NY, NX), 0.05).cells.all()


# ----------------------------------------------------------------------- the candidate region: the cap and the shadow

from solvers.source_selection import shadow_whole  # noqa: E402


def test_a_mask_can_be_restricted_to_the_disc_of_the_cap():
    mask = GridMask.open((NY, NX)).within_disc(CENTER, 300.0)

    assert mask.contains(np.array([CENTER[0]]), np.array([CENTER[1]]))[0]
    assert not mask.contains(np.array([CENTER[0] + 400.0]), np.array([CENTER[1]]))[0]
    assert not mask.contains(np.array([CENTER[0] + 280.0]), np.array([CENTER[1] + 280.0]))[0]  # the box's corner, 396 away
    assert 0.7 * np.pi * 300**2 < mask.total_area < 1.4 * np.pi * 300**2


def test_the_pixels_of_a_box_are_the_masks_cells_scaled_up():
    mask = _band_mask(700, 1300)

    pixels = mask.pixels((640, 0, 1360, 64))

    assert pixels.shape == (64, 720)
    assert not pixels[:, 70:600].any() and pixels[:, :40].all() and pixels[:, 690:].all()


def test_a_shadow_is_whole_when_both_its_edges_are_in_the_frame():
    assert shadow_whole(_vertical_shadow(CENTER[0], 450.0), (NY, NX))
    assert not shadow_whole(_vertical_shadow(NX - 100.0, 450.0), (NY, NX))  # its far side is off the frame
    assert not shadow_whole(None, (NY, NX))
    open_sided = _vertical_shadow(CENTER[0], 450.0)
    open_sided.penumbra_s = (-450.0, float("inf"))
    assert not shadow_whole(open_sided, (NY, NX))
    closed = _vertical_shadow(CENTER[0], 450.0)
    closed.penumbra_s = (-450.0, 450.0)
    assert shadow_whole(closed, (NY, NX))


def test_nothing_is_detected_or_chosen_beyond_the_cap():
    frame, _ = _stars(n=150)
    params = SelectionParams(seeing_scale=1.0, n_target=12, min_snr=20.0, box_size=50)  # the default cap: 1.7 low-coma radii

    sel = select_sources(frame, optical_center=CENTER, low_coma_radius=200.0, params=params)

    assert sel.reach_px == pytest.approx(340.0) and not sel.shadow_whole
    assert np.hypot(sel.x - CENTER[0], sel.y - CENTER[1]).max() < 340.0 + 40  # a margin for the detected edge
    assert (np.hypot(sel.x[sel.kept] - CENTER[0], sel.y[sel.kept] - CENTER[1]) <= 340.0).all()
    assert (
        sel.area_measured_px < 0.6 * NX * NY
    )  # the disc is a tenth of the frame; the rest is padding and the steps' repeats


def test_too_few_stars_in_the_candidate_region_is_a_failure_not_a_search_of_the_frame():
    frame, _ = _stars(n=150)
    params = SelectionParams(seeing_scale=1.0, n_target=30, fail_below=8, min_snr=20.0, box_size=50, reach_factor=1.0)

    sel = select_sources(frame, optical_center=CENTER, low_coma_radius=60.0, params=params)

    assert sel.status == "too_few_sources" and sel.window_box[2] - sel.window_box[0] < 400


def test_a_whole_shadow_gets_the_larger_cap():
    frame, _ = _stars(n=150)
    shadow = _vertical_shadow(CENTER[0], 450.0)
    params = SelectionParams(seeing_scale=1.0, n_target=20, min_snr=20.0, box_size=50)

    sel = select_sources(frame, shadow=shadow, optical_center=CENTER, low_coma_radius=300.0, params=params)

    assert sel.shadow_whole and sel.reach_px == pytest.approx(2.5 * 300.0)
    assert not (np.abs(sel.x - CENTER[0]) < 400).any()  # nothing in the band
    assert sel.kept.sum() > 0


def test_a_closed_shadow_is_cleared_on_both_sides_by_the_first_window():
    mask = _band_mask(700, 1300).within_disc(CENTRE, 1500.0)
    plain = window_schedule(mask, CENTRE, 100.0, 1.25, reach_limit=1500.0)
    balanced = window_schedule(mask, CENTRE, 100.0, 1.25, reach_limit=1500.0, balanced=True)

    first = balanced[0]
    # both flanks are in the first window, not only the nearer one
    left = GridMask(mask.cells & (np.arange(mask.cells.shape[1]) * 16 < 700)[None, :], 16, SHAPE)
    right = GridMask(mask.cells & (np.arange(mask.cells.shape[1]) * 16 >= 1300)[None, :], 16, SHAPE)
    assert left.area(first.box) > 0 and right.area(first.box) > 0
    assert first.half_size >= plain[0].half_size


def test_the_candidate_region_ends_the_schedule():
    mask = GridMask.open(SHAPE).within_disc(CENTRE, 400.0)

    windows = window_schedule(mask, CENTRE, 100.0, 1.25, reach_limit=400.0)

    assert windows[-1].exhausted and windows[-1].half_size <= 400.0 + 1e-9
    assert all(w.half_size <= 400.0 + 1e-9 for w in windows)
