"""Tests for imaging.frame_shift -- the spiral search's measurement step.

The one that earns its keep is the sign convention. ``phase_cross_correlation`` returns
the shift required to register the moving image back onto the reference, which is the
NEGATIVE of how the content moved; ``measure_shift`` negates it so callers read "the sky
moved this way". Get that backwards and you still get a plausible number, pointing the
wrong way -- the worst possible failure for something an operator will act on.

The second is fixed-pattern noise. The mount moves between the two frames but the
detector does not, so hot pixels sit at identical coordinates in both and correlate
perfectly at zero shift. On a sparse field that artefact can beat the real peak.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("skimage", reason="scikit-image unavailable")
pytest.importorskip("photutils", reason="photutils unavailable")
from scipy.ndimage import shift as ndi_shift

from imaging.frame_shift import ShiftResult, max_reliable_shift, measure_shift

SIZE = 320
STAR_POSITIONS = [(70, 90), (150, 230), (240, 60), (110, 175), (200, 260)]


def star_field(seed: int = 0) -> np.ndarray:
    """A synthetic sky: gaussian stars on read noise."""
    rng = np.random.default_rng(seed)
    data = rng.normal(100.0, 5.0, (SIZE, SIZE)).astype(np.float32)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    for y, x in STAR_POSITIONS:
        data += 3000.0 * np.exp(-(((yy - y) ** 2 + (xx - x) ** 2) / (2 * 3.0**2)))
    return data


def move(data: np.ndarray, dy: float, dx: float) -> np.ndarray:
    """Move the CONTENT by (dy, dx) -- positive dx moves it along +x."""
    return ndi_shift(data, shift=(dy, dx), order=3, mode="nearest").astype(np.float32)


class TestSignConvention:
    """dx/dy report how the CONTENT moved, not how to undo the move."""

    @pytest.mark.parametrize(
        ("dy", "dx"),
        [(0.0, 12.0), (0.0, -12.0), (7.0, 0.0), (-7.0, 0.0), (5.0, -9.0)],
        ids=["+x", "-x", "+y", "-y", "mixed"],
    )
    def test_reports_how_the_content_moved(self, dy, dx):
        reference = star_field()
        final = move(reference, dy, dx)

        result = measure_shift(reference, final)

        assert result.dx == pytest.approx(dx, abs=0.3), "dx must have the sign of the content's motion"
        assert result.dy == pytest.approx(dy, abs=0.3), "dy must have the sign of the content's motion"

    def test_no_motion_reports_zero(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy())
        assert result.dx == pytest.approx(0.0, abs=0.05)
        assert result.dy == pytest.approx(0.0, abs=0.05)
        assert result.at_origin, "an exact null must be flagged, since it is also the artefact signature"


class TestFixedPatternNoise:
    def test_hot_pixels_do_not_win_the_correlation(self):
        """Hot pixels are at the SAME detector coordinates in both frames.

        Without the high-pass in _prepare they plant a peak at exactly (0, 0) and can
        beat the real one on a sparse field -- reporting "the sky did not move" for a
        mount that demonstrably did.
        """
        reference = star_field()
        final = move(reference, 6.0, 11.0)

        rng = np.random.default_rng(7)
        hot_y = rng.integers(40, SIZE - 40, 300)
        hot_x = rng.integers(40, SIZE - 40, 300)
        for frame in (reference, final):  # identical positions in both, as on a real detector
            frame[hot_y, hot_x] = 60000.0

        result = measure_shift(reference, final)

        assert not result.at_origin, "the fixed pattern won the correlation"
        assert result.dx == pytest.approx(11.0, abs=0.5)
        assert result.dy == pytest.approx(6.0, abs=0.5)


class TestCropping:
    def test_crop_fraction_sets_the_correlated_area(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy(), crop_fraction=0.5)
        expected = (SIZE // 2 // 2) * 2  # halved, then rounded to an even span by _prepare
        assert result.crop_shape == (expected, expected)
        assert result.crop_fraction == 0.5

    def test_default_crop_avoids_the_coma_heavy_edges(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy())
        assert result.crop_fraction == pytest.approx(0.66)
        assert result.crop_shape[0] < SIZE, "the outer field must be excluded"

    def test_a_shift_still_measures_correctly_under_a_tight_crop(self):
        reference = star_field()
        final = move(reference, 4.0, 8.0)
        result = measure_shift(reference, final, crop_fraction=0.5)
        assert result.dx == pytest.approx(8.0, abs=0.5)
        assert result.dy == pytest.approx(4.0, abs=0.5)


class TestGuards:
    def test_mismatched_shapes_raise(self):
        with pytest.raises(ValueError, match="differ in shape"):
            measure_shift(star_field(), np.zeros((SIZE, SIZE // 2), dtype=np.float32))

    def test_result_is_json_ready(self):
        """The operator's result.json is built from this."""
        result = measure_shift(star_field(), star_field())
        as_dict = result.as_dict()
        assert set(as_dict) >= {"dx", "dy", "error", "crop_fraction", "crop_shape", "at_origin"}
        assert isinstance(as_dict["dx"], float) and isinstance(as_dict["dy"], float)

    def test_values_are_rounded_for_reporting(self):
        """Two decimals: seeing and tracking drift dominate long before 1/100 px."""
        result = measure_shift(star_field(), move(star_field(), 3.0, 5.0))
        assert result.dx == round(result.dx, 2)
        assert result.dy == round(result.dy, 2)

    def test_max_reliable_shift_scales_with_the_crop(self):
        assert max_reliable_shift((5644, 8288), 0.66) == pytest.approx(8288 * 0.66 / 3)
        assert max_reliable_shift((5644, 8288), 0.5) < max_reliable_shift((5644, 8288), 0.66)

    def test_shift_result_is_a_plain_dataclass(self):
        assert isinstance(measure_shift(star_field(), star_field()), ShiftResult)
