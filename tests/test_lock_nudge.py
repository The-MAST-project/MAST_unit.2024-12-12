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
    `get_limit_frame()` -- what PHD2 itself reports holding (#245) --
    and never from config, or it is wrong by 7 px in x and 1 in y: small enough to
    survive review, and a real pointing error on every nudge.
    """

    def test_a_configured_rectangle_is_not_the_one_phd2_receives(self):
        configured = ImagerRoi(x=521, y=1, width=7759, height=4811)
        verbatim = ImagerRoi.verbatim(x=521, y=1, width=7759, height=4811)
        assert (configured.x, configured.y) == (528, 2)
        assert (verbatim.x, verbatim.y) == (521, 1)

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


def _frame(path, size: tuple[int, int], star: tuple[float, float]) -> str:
    """A guide frame, as PHD2 saves it, with one star at `star` (x, y) on a flat sky."""
    import numpy as np
    from astropy.io import fits

    width, height = size
    y, x = np.mgrid[0:height, 0:width]
    sky = 100.0 + np.random.default_rng(0).normal(0.0, 3.0, (height, width))
    data = sky + 5000.0 * np.exp(-((x - star[0]) ** 2 + (y - star[1]) ** 2) / (2 * 2.0**2))
    fits.PrimaryHDU(data.astype(np.float32)).writeto(path)
    return str(path)


class TestASolvedNudgeIsApplied:
    """The path past a successful solve, end to end with the PHD2 and solver faked.

    The telemetry once read `connector.limit_frame_in_force`, an attribute #245
    removed. The AttributeError came after the solve and before the lock moved, and
    the handover catches everything around the nudge -- so a nudge that solved was
    never applied, and the log said only "lock nudge failed".

    The offset is applied to where the star WAS in the solved frame, not to the lock.
    The solve says where the fiber sat when that frame was taken, and the star sat
    wherever the guide error had put it then -- 0.7-3.2 px off the lock on
    2026-10-20. Added to the lock instead, that error stays in.
    """

    @pytest.fixture(autouse=True)
    def _solver_import(self):
        try:
            import solvers.mastrometry  # noqa: F401 -- the nudge imports it for DOWNSAMPLE_FACTOR
        except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
            pytest.skip(f"solver import chain unavailable here ({ex!r})")

    def nudge(self, image_path, lock=(100.0, 100.0), dec_offset_arcsec=0.0):
        from types import SimpleNamespace

        from astropy.coordinates import Angle

        from common.config.phd2 import LockNudgeConfig
        from common.interfaces.solving import SolvingResult
        from common.utils import Coord
        from lock_nudge import nudge_lock_to_target

        moved: list[tuple[float, float]] = []
        sol = solution(dec_degs=30.0)
        sol.ra_hours = 15.0
        sol.dec_rads = math.radians(30.0)
        connector = SimpleNamespace(
            get_lock_position=lambda: lock,
            get_limit_frame=lambda: None,
            save_image=lambda: image_path,
            set_lock_position=lambda x, y: moved.append((x, y)),
            avg_dist=0.1,
        )
        unit = SimpleNamespace(
            solver=SimpleNamespace(
                _backend=SimpleNamespace(solve=lambda **kwargs: SolvingResult(succeeded=True, solution=sol))
            )
        )
        target = Coord(ra=Angle(15.0, unit="hour"), dec=Angle(30.0 + dec_offset_arcsec / 3600.0, unit="deg"))
        outcome = nudge_lock_to_target(unit, connector, target, LockNudgeConfig(enabled=True, confirm_frames=0))
        return outcome, moved

    def test_the_lock_moves(self, tmp_path):
        # one arcsecond of dec off: a few pixels, well inside max_offset_px
        outcome, moved = self.nudge(_frame(tmp_path / "f.fits", (200, 200), (100.0, 100.0)), dec_offset_arcsec=1.0)
        assert outcome.applied, outcome.reason
        assert len(moved) == 1

    @pytest.mark.parametrize(
        ("size", "star"),
        [((8288, 5644), (6102.4, 2561.7)), ((300, 200), (151.6, 98.3))],
        ids=["full-sensor", "limit-frame"],
    )
    def test_on_target_the_lock_goes_to_the_star_not_back_to_itself(self, tmp_path, size, star):
        """Zero sky offset isolates the reference: the new lock is the star's position.

        The frame PHD2 saves is the frame it delivers -- the limit frame when one is in
        force -- so a position measured in it is in the lock's own coordinates.
        """
        lock = (round(star[0]) - 2.0, round(star[1]) + 1.0)
        outcome, moved = self.nudge(_frame(tmp_path / "f.fits", size, star), lock=lock)
        assert outcome.applied, outcome.reason
        assert moved[0] == pytest.approx(star, abs=0.1)

    def test_no_star_near_the_lock_leaves_it_alone(self, tmp_path):
        outcome, moved = self.nudge(_frame(tmp_path / "f.fits", (200, 200), (20.0, 20.0)), lock=(150.0, 150.0))
        assert not outcome.applied
        assert moved == []

    def test_a_star_too_far_from_the_lock_is_refused(self, tmp_path):
        """A hot pixel, or a cosmic ray in a frame the star dropped out of, can sit anywhere
        in the search box; max_offset_px bounds the move the lock would make, not only D."""
        outcome, moved = self.nudge(_frame(tmp_path / "f.fits", (200, 200), (110.0, 108.0)), lock=(100.0, 100.0))
        assert not outcome.applied
        assert moved == []
