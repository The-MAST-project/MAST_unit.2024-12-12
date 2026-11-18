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
