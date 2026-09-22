"""The lock nudge re-references guiding to the target before the mirror goes in (#19).

Two things here can be wrong in ways no reading catches, so both are pinned:

- the **coordinate frame**. PHD2 validates a lock position against the image it is
  currently delivering, which is the limit frame when one is in force. A position
  derived from a plate solve is in full-sensor pixels, and sending it unconverted
  lands one crop origin away -- and *succeeds*, because that is a valid image
  coordinate. Same trap as #234, in a place where it moves the mount.
- the **sky-to-pixel arithmetic**: parity, the cos(dec) factor on RA, and the
  solver's downsampling, each of which is silently wrong-by-a-constant rather than
  loud.
"""

import math

import pytest

from common.interfaces.solving import SolvingSolution
from common.models.statuses import ImagerRoi
from lock_nudge import sky_offset_to_detector_pixels

PIXEL_SCALE_DEG = 0.27 / 3600.0  # per detector pixel
DOWNSAMPLE = 2


def solution(rotation_deg: float = 0.0, parity: int = 1, dec_degs: float = 0.0) -> SolvingSolution:
    """A CD matrix per DOWNSAMPLED pixel, as the backend reports it."""
    scale = PIXEL_SCALE_DEG * DOWNSAMPLE
    r = math.radians(rotation_deg)
    return SolvingSolution(
        dec_degs=dec_degs,
        cd1_1=parity * scale * math.cos(r),
        cd1_2=-scale * math.sin(r),
        cd2_1=parity * scale * math.sin(r),
        cd2_2=scale * math.cos(r),
    )


class TestSkyOffsetToDetectorPixels:
    def test_one_pixel_of_sky_is_one_detector_pixel(self):
        """A 0.27" offset is one detector pixel, not one downsampled pixel."""
        dx, dy = sky_offset_to_detector_pixels(solution(), 0.27, 0.0, 0.0, DOWNSAMPLE)
        assert dx == pytest.approx(1.0, abs=1e-9)
        assert dy == pytest.approx(0.0, abs=1e-9)

    def test_declination_offset_maps_to_the_other_axis(self):
        dx, dy = sky_offset_to_detector_pixels(solution(), 0.0, 0.27, 0.0, DOWNSAMPLE)
        assert dx == pytest.approx(0.0, abs=1e-9)
        assert dy == pytest.approx(1.0, abs=1e-9)

    def test_ra_offset_carries_cos_dec(self):
        """solve_and_correct's RA delta is plain; the CD matrix wants standard coordinates.

        At dec=60 the same RA offset spans half the angle on the sky, so it must come
        out as half the pixels.
        """
        dx, _ = sky_offset_to_detector_pixels(solution(dec_degs=60.0), 0.54, 0.0, 60.0, DOWNSAMPLE)
        assert dx == pytest.approx(1.0, abs=1e-6)

    def test_rotation_is_honored(self):
        """At -22 deg field rotation an RA-only offset lands on both axes."""
        dx, dy = sky_offset_to_detector_pixels(solution(rotation_deg=-22.0), 2.7, 0.0, 0.0, DOWNSAMPLE)
        assert math.hypot(dx, dy) == pytest.approx(10.0, abs=1e-6)
        assert dy == pytest.approx(-10.0 * math.sin(math.radians(-22.0)), abs=1e-6)

    def test_parity_flips_the_ra_axis_only(self):
        """A mirrored field inverts x and leaves the magnitude right -- the quiet error."""
        dx, dy = sky_offset_to_detector_pixels(solution(parity=1), 2.7, 0.0, 0.0, DOWNSAMPLE)
        mdx, mdy = sky_offset_to_detector_pixels(solution(parity=-1), 2.7, 0.0, 0.0, DOWNSAMPLE)
        assert mdx == pytest.approx(-dx, abs=1e-9)
        assert mdy == pytest.approx(dy, abs=1e-9)

    def test_a_degenerate_cd_matrix_is_refused(self):
        assert (
            sky_offset_to_detector_pixels(
                SolvingSolution(cd1_1=0.0, cd1_2=0.0, cd2_1=0.0, cd2_2=0.0), 1.0, 1.0, 0.0, DOWNSAMPLE
            )
            is None
        )

    def test_a_missing_cd_matrix_is_refused(self):
        assert sky_offset_to_detector_pixels(SolvingSolution(), 1.0, 1.0, 0.0, DOWNSAMPLE) is None


class TestImagerRoiConditioning:
    """Why the nudge works in offsets rather than absolute positions.

    `ImagerRoi` conditions a rectangle to the camera's alignment constraints, so a
    configured 520,0 reaches PHD2 as 527,1. Any future version that forms an
    absolute lock position must take its crop origin from
    `limit_frame_in_force` -- the connector's record of what it actually sent --
    and never from config, or it is wrong by 7 px in x and 1 in y: small enough to
    survive review, and a real pointing error on every nudge.
    """

    def test_a_configured_rectangle_is_not_the_one_phd2_receives(self):
        configured = ImagerRoi(x=520, y=0, width=7760, height=4812)
        verbatim = ImagerRoi.verbatim(x=520, y=0, width=7760, height=4812)
        assert (configured.x, configured.y) == (527, 1)
        assert (verbatim.x, verbatim.y) == (520, 0)

    def test_an_offset_is_unchanged_by_the_crop(self):
        """The property the nudge rests on: origin cancels in a difference."""
        roi = ImagerRoi.verbatim(x=520, y=0, width=7760, height=4812)
        a_sensor, b_sensor = (6067.0, 2560.0), (6070.0, 2564.0)
        a_image = (a_sensor[0] - roi.x, a_sensor[1] - roi.y)
        b_image = (b_sensor[0] - roi.x, b_sensor[1] - roi.y)
        assert (b_sensor[0] - a_sensor[0], b_sensor[1] - a_sensor[1]) == (
            b_image[0] - a_image[0],
            b_image[1] - a_image[1],
        )
