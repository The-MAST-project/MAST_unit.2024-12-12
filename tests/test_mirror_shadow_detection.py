"""`detect_mirror_shadow` on the two cases real frames showed it getting wrong (see DECISIONS.md, 2026-11-18)."""

from __future__ import annotations

import numpy as np
import pytest

from imaging.mirror_shadow import detect_mirror_shadow

NX, NY = 4000, 2800


def _frame(column_profile: np.ndarray, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return column_profile[None, :] * (1.0 + rng.normal(0.0, 0.01, (NY, NX)))


def _x() -> np.ndarray:
    return np.arange(NX, dtype=float) - NX / 2


def _centred_bowl(depth: float, half_width: float) -> np.ndarray:
    """Sky of 2000, with a smooth bowl of `depth` and `half_width` (pixels) in the middle."""
    return 2000.0 * (1.0 - depth * np.clip(1.0 - (_x() / half_width) ** 2, 0.0, None))


def _edge_rolloff(depth: float, start: float = 600.0) -> np.ndarray:
    """Sky of 2000 falling off over the last `start` pixels toward the right edge, down by `depth`."""
    x = _x() + NX / 2
    ramp = np.clip((x - (NX - start)) / start, 0.0, 1.0)
    return 2000.0 * (1.0 - depth * ramp)


def test_a_wide_shallow_central_bowl_is_found():
    """A 25% deep bowl covering ~45% of the profile: the whole-profile spread used to be its own noise."""
    found = detect_mirror_shadow(_frame(_centred_bowl(0.25, 900.0)))
    assert found.present
    assert abs(abs(found.tilt_deg) - 90.0) < 5.0  # the angle follows the band, not the lowest noise
    assert found.depth == pytest.approx(0.25, abs=0.05)
    assert abs(found.offset) < 200
    assert found.prominence > 6


def test_a_flat_frame_has_no_shadow():
    assert not detect_mirror_shadow(_frame(np.full(NX, 2000.0))).present


def test_an_edge_rolloff_is_not_a_shadow_when_the_search_is_centred():
    frame = _frame(_edge_rolloff(0.35))
    assert not detect_mirror_shadow(frame, max_center_offset=1200.0).present


def test_the_search_is_unrestricted_by_default():
    """The stage-geometry calibration moves the shadow across the frame and needs to find it anywhere."""
    frame = _frame(2000.0 * (1.0 - 0.5 * np.clip(1.0 - ((_x() - 1500.0) / 500.0) ** 2, 0.0, None)))
    found = detect_mirror_shadow(frame)
    assert found.present
    assert 1000 < abs(found.offset) < 2000
    assert not detect_mirror_shadow(frame, max_center_offset=1200.0).present


def test_a_central_band_is_still_found_with_the_centred_search():
    assert detect_mirror_shadow(_frame(_centred_bowl(0.3, 800.0)), max_center_offset=1200.0).present


def _slope_through_the_middle() -> np.ndarray:
    """Bright on the left, darker on the right, the fall spread across the middle: no band, no recovery."""
    x = _x()
    return 2000.0 * (1.0 - 0.4 * np.clip((x + 1200.0) / 2400.0, 0.0, 1.0))


def test_a_slope_has_a_centre_but_no_second_side():
    frame = _frame(_slope_through_the_middle())
    centred = detect_mirror_shadow(frame, max_center_offset=1200.0)
    assert centred.present  # the centred search alone is fooled: the middle of the slope is "centred"
    assert centred.two_sided_depth < 0.04
    assert not detect_mirror_shadow(frame, max_center_offset=1200.0, require_two_sided=True).present


def test_a_real_band_recovers_on_both_sides():
    found = detect_mirror_shadow(_frame(_centred_bowl(0.25, 900.0)), max_center_offset=1200.0, require_two_sided=True)
    assert found.present
    assert found.two_sided_depth > 0.1


# -- the shadow can be anywhere across the frame, and any size: full, or partial at either vertical edge -----

MX, MY = 2000, 1400  # half the size of the frames above, so the matrix is quick; shadows are scaled with it


def _shadow_frame(centre: float, umbra_half: float, penumbra_half: float, depth: float = 0.4, seed: int = 7) -> np.ndarray:
    """Flat sky with a vertical shadow: `depth` deep within `umbra_half` of `centre` (px from the frame's middle),
    ramping linearly back to the sky over the next `penumbra_half - umbra_half` px. Some of it may lie outside."""
    rng = np.random.default_rng(seed)
    d = np.abs(np.arange(MX, dtype=float) - MX / 2 - centre)
    ramp = np.clip((penumbra_half - d) / max(penumbra_half - umbra_half, 1.0), 0.0, 1.0)
    return 2000.0 * (1.0 - depth * ramp)[None, :] * (1.0 + rng.normal(0.0, 0.01, (MY, MX)))


def _true_columns(centre: float, penumbra_half: float) -> set[int]:
    return set(range(max(0, int(MX / 2 + centre - penumbra_half)), min(MX - 1, int(MX / 2 + centre + penumbra_half)) + 1))


def _found_columns(model) -> set[int]:
    return set(np.flatnonzero(model.mask(include_penumbra=True).any(axis=0)).tolist())


@pytest.mark.parametrize("umbra_half,penumbra_half", [(75.0, 250.0), (250.0, 500.0), (500.0, 750.0)])
@pytest.mark.parametrize("centre", [-1750.0, -1250.0, -750.0, 0.0, 750.0, 1250.0, 1750.0])
def test_a_shadow_is_found_wherever_it_is_and_whatever_its_size(centre, umbra_half, penumbra_half):
    """Full or partial, at either edge or between: found, and masked over the columns it really covers.

    A shadow whose visible part is only the faint outer end of its penumbra (centre far outside the frame with a
    narrow penumbra) is below what can be seen; those cases are not asserted.
    """
    visible = 0.4 * np.clip((MX / 2 - (abs(centre) - penumbra_half)) / max(penumbra_half - umbra_half, 1.0), 0.0, 1.0)
    if abs(centre) - penumbra_half > MX / 2 - 50 or visible < 0.12:
        pytest.skip("essentially outside the frame")
    frame = _shadow_frame(centre, umbra_half, penumbra_half)
    for flipped in (False, True):
        image = frame[:, ::-1].copy() if flipped else frame
        truth = _true_columns(-centre if flipped else centre, penumbra_half)
        found = detect_mirror_shadow(image)
        assert found.present, f"missed: centre {centre}, umbra {umbra_half}, penumbra {penumbra_half}, flipped {flipped}"
        got = _found_columns(found)
        iou = len(truth & got) / max(len(truth | got), 1)
        assert iou > 0.6, f"mask covers the wrong columns (IoU {iou:.2f}): centre {centre}, flipped {flipped}"


def _leaning_band(lean_deg: float, depth: float = 0.55, umbra: float = 620.0, penumbra: float = 990.0) -> np.ndarray:
    """A wide, flat-bottomed band (linear ramps between the umbra and penumbra edges) that leans `lean_deg` from
    vertical, on a sky that is 6% brighter at the top than at the bottom, as a real frame's is."""
    rng = np.random.default_rng(5)
    yy, xx = np.mgrid[0:NY, 0:NX].astype(float)
    across = np.abs((xx - NX / 2) - np.tan(np.deg2rad(lean_deg)) * (yy - NY / 2))
    dip = depth * np.clip((penumbra - across) / (penumbra - umbra), 0.0, 1.0)
    gradient = 1.0 + 0.06 * (1.0 - yy / NY)
    return 2000.0 * gradient * (1.0 - dip) * (1.0 + rng.normal(0.0, 0.01, (NY, NX)))


@pytest.mark.parametrize("lean", [0.0, 1.5, -3.0])
def test_the_tilt_of_a_wide_band_is_its_lean_not_the_angle_of_the_deepest_dip(lean):
    """On a real centred shadow 2600 px wide the dip was as deep at 9 degrees off vertical as at the 1 where the band
    leans (the angles tied within 1%), so the band was drawn 9 degrees tilted and the darkened frame still showed
    the old umbra and penumbra along one side. The tilt is now measured from how the band's profile moves down the
    frame."""
    found = detect_mirror_shadow(_leaning_band(lean))
    assert found.present
    measured = float(np.rad2deg(np.arctan(np.cos(found.angle) / np.sin(found.angle))))  # lean, + = right going down
    assert measured == pytest.approx(lean, abs=1.0)
    assert abs(found.offset) < 150


def test_the_old_sweep_alone_got_the_lean_of_a_wide_band_wrong_but_the_refinement_is_optional():
    frame = _leaning_band(1.5)
    sweep = detect_mirror_shadow(frame, refine_tilt=False)
    refined = detect_mirror_shadow(frame)
    lean = lambda m: float(np.rad2deg(np.arctan(np.cos(m.angle) / np.sin(m.angle))))  # noqa: E731
    assert abs(lean(refined) - 1.5) <= abs(lean(sweep) - 1.5)


def test_a_band_cut_by_the_frames_edge_is_open_on_that_side():
    """A shadow whose far side is outside the frame: the darkened band must run to the edge, and its umbra is not
    the visible run mirrored about a centre that is on the frame's edge (the umbra came out narrower than the part of
    it that is in the frame, and the right margin was drawn as penumbra)."""
    x = _x() + NX / 2
    ramp = np.clip((x - 2300.0) / 1200.0, 0.0, 1.0)  # a 1200 px penumbra, then the umbra to the frame's edge
    found = detect_mirror_shadow(_frame(2000.0 * (1.0 - 0.6 * ramp)))
    assert found.present and found.truncated
    umbra, penumbra = found.masks()
    assert umbra[:, -1].all()  # the edge column is umbra, not penumbra
    assert not penumbra[:, -1].any()
    # the umbra's open side is the right: pixels from the centre, + is right
    left, right = found._across_frame(found.umbra_s)
    assert np.isinf(right) and left == pytest.approx(2900.0 - NX / 2, abs=150)  # the 50% point of the ramp
    left_p, right_p = found._across_frame(found.penumbra_s)
    assert np.isinf(right_p) and left_p == pytest.approx(2300.0 - NX / 2 + 120, abs=200)  # the 10% point
    assert "right edge" in found.describe() and "cut by the frame" in found.describe()


def test_a_band_inside_the_frame_is_not_truncated_and_its_bands_are_asymmetric_only_as_measured():
    found = detect_mirror_shadow(_frame(_centred_bowl(0.25, 900.0)))
    assert found.present and not found.truncated
    left, right = found._across_frame(found.penumbra_s)
    assert np.isfinite(left) and np.isfinite(right) and left < 0 < right
    assert found.describe().startswith("umbra")


def test_cropping_moves_the_bounds_with_the_band():
    x = _x() + NX / 2
    found = detect_mirror_shadow(_frame(2000.0 * (1.0 - 0.6 * np.clip((x - 2300.0) / 1200.0, 0.0, 1.0))))
    crop = found.cropped(1000, 0, 3000, NY)
    assert crop.truncated
    # the same pixels are umbra in the crop as in the frame it was cut from
    np.testing.assert_array_equal(crop.masks()[0], found.masks()[0][:, 1000:4000])
