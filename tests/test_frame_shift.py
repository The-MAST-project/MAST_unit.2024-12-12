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

from imaging.frame_shift import MIN_CONFIDENCE, ShiftResult, max_reliable_shift, measure_shift

SIZE = 320
CENTER = SIZE // 2
STAR_POSITIONS = [(70, 90), (150, 230), (240, 60), (110, 175), (200, 260)]


def star_field(seed: int = 0, stars: bool = True) -> np.ndarray:
    """A synthetic sky: gaussian stars on read noise."""
    rng = np.random.default_rng(seed)
    data = rng.normal(100.0, 5.0, (SIZE, SIZE)).astype(np.float32)
    if stars:
        yy, xx = np.mgrid[0:SIZE, 0:SIZE]
        for y, x in STAR_POSITIONS:
            data += 3000.0 * np.exp(-(((yy - y) ** 2 + (xx - x) ** 2) / (2 * 3.0**2)))
    return data


def shadow_band(angle_deg: float = 10.0, offset: float = 20.0, depth: float = 0.35) -> np.ndarray:
    """The folding mirror's shadow: a wide, tilted band with a graded penumbra.

    Multiplicative and fixed in the DETECTOR frame, so it does not move with the sky --
    the same class of artefact as the hot pixels above, at a far larger scale.
    """
    umbra, penumbra = 40.0, 70.0
    angle = np.deg2rad(angle_deg)
    center_x, center_y = (SIZE - 1) / 2, (SIZE - 1) / 2
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    perpendicular = -(xx - center_x) * np.sin(angle) + (yy - center_y) * np.cos(angle)
    graded = np.clip((penumbra - np.abs(perpendicular - offset)) / (penumbra - umbra), 0, 1)
    return 1.0 - depth * graded


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

        result = measure_shift(reference, final, CENTER, CENTER)

        assert result.dx == pytest.approx(dx, abs=0.3), "dx must have the sign of the content's motion"
        assert result.dy == pytest.approx(dy, abs=0.3), "dy must have the sign of the content's motion"

    def test_no_motion_reports_zero(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy(), CENTER, CENTER)
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

        result = measure_shift(reference, final, CENTER, CENTER)

        assert not result.at_origin, "the fixed pattern won the correlation"
        assert result.dx == pytest.approx(11.0, abs=0.5)
        assert result.dy == pytest.approx(6.0, abs=0.5)

    @pytest.mark.parametrize("depth", [0.15, 0.35, 0.60], ids=["shallow", "typical", "deep"])
    def test_the_folding_mirror_shadow_does_not_win_either(self, depth):
        """The pick-off mirror's shadow band is also fixed in the detector frame.

        Unlike the hot pixels it needs no special handling: it is smooth and wide, so
        _bg_subtract's 64-px background model absorbs it. This test is what licenses NOT
        masking it -- masked correlation would cost sub-pixel precision (skimage ignores
        upsample_factor when masks are given), so the cheap option had better hold.
        """
        band = shadow_band(depth=depth)
        reference = (star_field() * band).astype(np.float32)
        final = (move(star_field(), 6.0, 11.0) * band).astype(np.float32)

        result = measure_shift(reference, final, CENTER, CENTER)

        assert result.dx == pytest.approx(11.0, abs=0.5)
        assert result.dy == pytest.approx(6.0, abs=0.5)
        assert result.confidence > MIN_CONFIDENCE


class TestConfidence:
    """skimage's own `error` return is a constant 1.0 under the default phase
    normalisation -- it discriminates nothing, which is why measure_shift computes its
    own. If this metric ever goes flat too, the operator has no tripwire left."""

    def test_a_real_match_scores_high(self):
        result = measure_shift(star_field(), move(star_field(), 6.0, 11.0), CENTER, CENTER)
        assert result.confidence > 0.9

    def test_uncorrelated_frames_score_near_zero(self):
        """Two starless frames: whatever shift comes back is read off noise."""
        result = measure_shift(star_field(1, stars=False), star_field(2, stars=False), CENTER, CENTER)
        assert result.confidence < MIN_CONFIDENCE

    def test_a_frame_with_no_stars_scores_near_zero(self):
        result = measure_shift(star_field(), star_field(3, stars=False), CENTER, CENTER)
        assert result.confidence < MIN_CONFIDENCE

    def test_identical_frames_score_one(self):
        assert measure_shift(star_field(), star_field().copy(), CENTER, CENTER).confidence == pytest.approx(1.0)


class TestCropping:
    def test_usable_fraction_sets_the_correlated_area(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy(), CENTER, CENTER, usable_fraction=0.5)
        expected = (int(SIZE * 0.5) // 2) * 2
        assert result.crop_shape == (expected, expected)
        assert result.usable_fraction == 0.5

    def test_default_window_avoids_the_coma_heavy_edges(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy(), CENTER, CENTER)
        assert result.usable_fraction == pytest.approx(0.66)
        assert result.crop_shape[0] < SIZE, "the outer field must be excluded"

    def test_the_window_is_centred_where_told_not_on_the_frame_centre(self):
        """The fibre is off-centre on the sensor, which is the whole point."""
        reference = star_field()
        off_centre = measure_shift(reference, reference.copy(), 100, 90, usable_fraction=0.4)
        assert off_centre.center_x == 100
        assert off_centre.center_y == 90

    def test_a_window_near_an_edge_is_clipped_not_out_of_bounds(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy(), 20, 20, usable_fraction=0.66)
        assert result.crop_shape[0] > 0 and result.crop_shape[1] > 0
        assert result.crop_shape[1] < int(SIZE * 0.66), "clipped against the sensor edge"

    def test_a_shift_still_measures_correctly_under_a_tight_crop(self):
        reference = star_field()
        final = move(reference, 4.0, 8.0)
        result = measure_shift(reference, final, CENTER, CENTER, usable_fraction=0.5)
        assert result.dx == pytest.approx(8.0, abs=0.5)
        assert result.dy == pytest.approx(4.0, abs=0.5)


class TestGuards:
    def test_mismatched_shapes_raise(self):
        with pytest.raises(ValueError, match="differ in shape"):
            measure_shift(star_field(), np.zeros((SIZE, SIZE // 2), dtype=np.float32), CENTER, CENTER)

    def test_result_is_json_ready(self):
        """The operator's result.json is built from this."""
        result = measure_shift(star_field(), star_field(), CENTER, CENTER)
        as_dict = result.as_dict()
        assert set(as_dict) >= {"dx", "dy", "confidence", "usable_fraction", "crop_shape", "at_origin"}
        assert isinstance(as_dict["dx"], float) and isinstance(as_dict["dy"], float)

    def test_values_are_rounded_for_reporting(self):
        """Two decimals: seeing and tracking drift dominate long before 1/100 px."""
        result = measure_shift(star_field(), move(star_field(), 3.0, 5.0), CENTER, CENTER)
        assert result.dx == round(result.dx, 2)
        assert result.dy == round(result.dy, 2)

    def test_max_reliable_shift_scales_with_the_crop(self):
        assert max_reliable_shift((5644, 8288), 0.66) == pytest.approx(8288 * 0.66 / 3)
        assert max_reliable_shift((5644, 8288), 0.5) < max_reliable_shift((5644, 8288), 0.66)

    def test_shift_result_is_a_plain_dataclass(self):
        assert isinstance(measure_shift(star_field(), star_field(), CENTER, CENTER), ShiftResult)
