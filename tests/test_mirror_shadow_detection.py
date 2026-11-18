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
