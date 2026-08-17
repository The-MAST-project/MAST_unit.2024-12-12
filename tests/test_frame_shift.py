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

from imaging.frame_shift import (
    MIN_CONFIDENCE,
    ShiftResult,
    margins_from_fraction,
    max_reliable_shift,
    measure_shift,
)

SIZE = 320
CENTER = SIZE // 2
#: Margins suited to this synthetic frame. The production defaults (1000, 300) are
#: sized for a 8288x5644 sensor and would leave nothing of a 320 px test frame.
MARGIN = 40
#: Spread so that even the tightest crop below still holds several stars. A crop that
#: keeps only one or two is not a small version of a real crop -- it is a different
#: problem, in which the correlation is fitting noise, and it produced sub-pixel biases
#: that looked like a defect in the method rather than in the fixture.
STAR_POSITIONS = [
    (70, 90),
    (150, 230),
    (240, 60),
    (110, 175),
    (200, 260),
    (140, 150),
    (170, 190),
    (130, 205),
    (190, 140),
    (160, 120),
    (120, 145),
    (210, 180),
    (90, 210),
    (250, 150),
    (60, 160),
    (175, 160),
    (145, 175),
    (185, 205),
    (205, 130),
    (125, 185),
]


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


def detector_pattern(strength: float = 1.0, seed: int = 3) -> np.ndarray:
    """Additive fixed pattern of the kind a real sensor carries.

    Crucially NOT just single hot pixels: those a 3x3 median removes outright, which is
    why the single-pixel test above passed even while real frames were failing. Clusters,
    column defects and dust survive the median, stay put while the sky moves, and so
    correlate at exactly zero shift.

    At `strength=1.0` the pattern carries about 0.6x the energy of the stars. Above ~2x
    NOTHING recovers the shift, plain correlation included -- so this is calibrated to
    the regime that distinguishes the two normalisations, not to the worst case.
    """
    rng = np.random.default_rng(seed)
    pattern = np.zeros((SIZE, SIZE), dtype=np.float32)
    for y, x in zip(rng.integers(20, SIZE - 20, 60), rng.integers(20, SIZE - 20, 60), strict=True):
        pattern[y, x] = 4000.0 * strength  # single hot pixels
    for y, x in zip(rng.integers(20, SIZE - 20, 40), rng.integers(20, SIZE - 20, 40), strict=True):
        pattern[y : y + 3, x : x + 3] = 1200.0 * strength  # clusters: a 3x3 median cannot remove these
    for x in rng.integers(20, SIZE - 20, 6):
        pattern[:, x] += 90.0 * strength  # column defects
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    for y, x in zip(rng.integers(40, SIZE - 40, 8), rng.integers(40, SIZE - 40, 8), strict=True):
        r = np.hypot(yy - y, xx - x)
        pattern -= 70.0 * strength * np.exp(-((r - 9.0) ** 2) / (2 * 3.0**2))  # dust donuts
    return pattern


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

        result = measure_shift(reference, final, CENTER, CENTER, MARGIN, MARGIN)

        assert result.dx == pytest.approx(dx, abs=0.3), "dx must have the sign of the content's motion"
        assert result.dy == pytest.approx(dy, abs=0.3), "dy must have the sign of the content's motion"

    def test_no_motion_reports_zero(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy(), CENTER, CENTER, MARGIN, MARGIN)
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

        result = measure_shift(reference, final, CENTER, CENTER, MARGIN, MARGIN)

        assert not result.at_origin, "the fixed pattern won the correlation"
        assert result.dx == pytest.approx(11.0, abs=0.5)
        assert result.dy == pytest.approx(6.0, abs=0.5)

    @pytest.mark.parametrize("strength", [0.5, 1.0, 1.5], ids=["light", "typical", "heavy"])
    def test_a_realistic_detector_pattern_does_not_win(self, strength):
        """The regression that real frames exposed and synthetic ones did not.

        The sky moves; the detector does not. Under phase normalisation this pattern
        wins outright -- on real MAST acquisition frames it hijacked 18 of 19 pairs,
        returning ~0 px for a field that had demonstrably moved 15-20 px. That is the
        worst possible answer for an operator: "you are already centred", stated
        confidently. Plain cross-correlation weights by signal power, so the stars win.

        Note the pattern is added AFTER the move, so it sits at identical coordinates in
        both frames -- as on a real sensor. Shifting a frame shifts its defects with it,
        which is exactly why every earlier synthetic test missed this.
        """
        pattern = detector_pattern(strength)
        reference = (star_field() + pattern).astype(np.float32)
        final = (move(star_field(), 6.0, 11.0) + pattern).astype(np.float32)

        result = measure_shift(reference, final, CENTER, CENTER, MARGIN, MARGIN)

        assert not result.at_origin, "the fixed pattern won the correlation"
        assert result.dx == pytest.approx(11.0, abs=1.0)
        assert result.dy == pytest.approx(6.0, abs=1.0)

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

        result = measure_shift(reference, final, CENTER, CENTER, MARGIN, MARGIN)

        assert result.dx == pytest.approx(11.0, abs=0.5)
        assert result.dy == pytest.approx(6.0, abs=0.5)
        assert result.confidence > MIN_CONFIDENCE


class TestConfidence:
    """skimage's own `error` return is a constant 1.0 under the default phase
    normalisation -- it discriminates nothing, which is why measure_shift computes its
    own. If this metric ever goes flat too, the operator has no tripwire left."""

    def test_a_real_match_scores_high(self):
        result = measure_shift(star_field(), move(star_field(), 6.0, 11.0), CENTER, CENTER, MARGIN, MARGIN)
        assert result.confidence > 0.9

    def test_uncorrelated_frames_score_near_zero(self):
        """Two starless frames: whatever shift comes back is read off noise."""
        result = measure_shift(star_field(1, stars=False), star_field(2, stars=False), CENTER, CENTER, MARGIN, MARGIN)
        assert result.confidence < MIN_CONFIDENCE

    def test_a_frame_with_no_stars_scores_near_zero(self):
        result = measure_shift(star_field(), star_field(3, stars=False), CENTER, CENTER, MARGIN, MARGIN)
        assert result.confidence < MIN_CONFIDENCE

    def test_identical_frames_score_one(self):
        assert measure_shift(star_field(), star_field().copy(), CENTER, CENTER, MARGIN, MARGIN).confidence == pytest.approx(
            1.0
        )


class TestCropping:
    """Margins are the internal representation; a fraction converts into them."""

    def test_margins_set_the_correlated_area(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy(), CENTER, CENTER, margin_horizontal=40, margin_vertical=40)
        expected = (SIZE // 2 - 40) * 2
        assert result.crop_shape == (expected, expected)
        assert result.margin_horizontal == 40

    def test_a_fraction_converts_to_the_equivalent_margins(self):
        assert margins_from_fraction((SIZE, SIZE), 0.5) == (SIZE // 4, SIZE // 4)
        assert margins_from_fraction((SIZE, SIZE), 1.0) == (0, 0)
        h, v = margins_from_fraction((100, 400), 0.5)
        assert (h, v) == (100, 25), "each axis is trimmed by its own size, not a shared one"

    def test_the_window_is_centred_where_told_not_on_the_frame_centre(self):
        """The fibre is off-centre on the sensor, which is the whole point."""
        reference = star_field()
        off_centre = measure_shift(reference, reference.copy(), 100, 90, margin_horizontal=60, margin_vertical=60)
        assert off_centre.center_x == 100
        assert off_centre.center_y == 90

    def test_a_window_near_an_edge_is_clipped_not_out_of_bounds(self):
        reference = star_field()
        result = measure_shift(reference, reference.copy(), 20, 20, margin_horizontal=40, margin_vertical=40)
        assert result.crop_shape[0] > 0 and result.crop_shape[1] > 0
        assert result.crop_shape[1] < (SIZE // 2 - 40) * 2, "clipped against the sensor edge"

    def test_margins_larger_than_the_frame_still_leave_a_window(self):
        """A misconfigured margin must not produce an empty or inverted slice."""
        reference = star_field()
        result = measure_shift(reference, reference.copy(), CENTER, CENTER, margin_horizontal=99999, margin_vertical=99999)
        assert result.crop_shape[0] > 0 and result.crop_shape[1] > 0

    def test_a_shift_still_measures_correctly_under_a_tight_crop(self):
        reference = star_field()
        final = move(reference, 4.0, 8.0)
        result = measure_shift(reference, final, CENTER, CENTER, margin_horizontal=80, margin_vertical=80)
        assert result.dx == pytest.approx(8.0, abs=0.5)
        assert result.dy == pytest.approx(4.0, abs=0.5)


class TestGuards:
    def test_mismatched_shapes_raise(self):
        with pytest.raises(ValueError, match="differ in shape"):
            measure_shift(star_field(), np.zeros((SIZE, SIZE // 2), dtype=np.float32), CENTER, CENTER, MARGIN, MARGIN)

    def test_result_is_json_ready(self):
        """The operator's result.json is built from this."""
        result = measure_shift(star_field(), star_field(), CENTER, CENTER, MARGIN, MARGIN)
        as_dict = result.as_dict()
        assert set(as_dict) >= {"dx", "dy", "confidence", "margin_horizontal", "crop_shape", "at_origin"}
        assert isinstance(as_dict["dx"], float) and isinstance(as_dict["dy"], float)

    def test_values_are_rounded_for_reporting(self):
        """Two decimals: seeing and tracking drift dominate long before 1/100 px."""
        result = measure_shift(star_field(), move(star_field(), 3.0, 5.0), CENTER, CENTER, MARGIN, MARGIN)
        assert result.dx == round(result.dx, 2)
        assert result.dy == round(result.dy, 2)

    def test_max_reliable_shift_scales_with_the_crop(self):
        assert max_reliable_shift((5644, 8288), 1000) == pytest.approx((8288 - 2000) / 3)
        assert max_reliable_shift((5644, 8288), 2000) < max_reliable_shift((5644, 8288), 1000)
        assert max_reliable_shift((5644, 8288), 99999) > 0, "a bad margin must not give a negative limit"

    def test_shift_result_is_a_plain_dataclass(self):
        assert isinstance(measure_shift(star_field(), star_field(), CENTER, CENTER, MARGIN, MARGIN), ShiftResult)
