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
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.coordinates import Angle
from astropy.io import fits

from common.interfaces.solving import SolvingSolution
from common.models.statuses import ImagerRoi
from lock_nudge import detector_pixel_scale_arcsec, sky_offset_to_detector_pixels

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


def test_the_pixel_scale_is_per_detector_pixel():
    assert detector_pixel_scale_arcsec(solution(rotation_deg=-22.0, parity=-1), DOWNSAMPLE) == pytest.approx(0.27)


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


SENSOR = (8288, 5644)
MATCHING = object()  # FakePHD2's limit frame: whatever its frame size implies
CADENCE_S = 6.0  # one GuideStep every 6 s, inside the 4.6-9.1 s measured


class FakeClock:
    """`lock_nudge`'s clock: sleeping advances it, so timeouts pass without waiting."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    import lock_nudge

    fake = FakeClock()
    monkeypatch.setattr(lock_nudge.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(lock_nudge.time, "sleep", fake.sleep)
    return fake


def _frame_data(size: tuple[int, int], star: tuple[float, float]) -> np.ndarray:
    """A guide frame with one star at `star` (x, y) on a flat, noisy sky."""
    width, height = size
    y, x = np.mgrid[0:height, 0:width]
    sky = 100.0 + np.random.default_rng(0).normal(0.0, 3.0, (height, width))
    return (sky + 5000.0 * np.exp(-((x - star[0]) ** 2 + (y - star[1]) ** 2) / (2 * 2.0**2))).astype(np.float32)


class FakePHD2:
    """The connector as the nudge sees it.

    A GuideStep arrives every `CADENCE_S` of the fake clock (`latest_guide_step`). `save_image` writes the
    latest guide frame, stamped with the next of `dates` (the last repeats once they
    run out), as PHD2 hands back the most recent frame whenever it is asked.
    `step_px[i]` is the guide distance of step i, counted from the first. By default
    a frame smaller than the sensor is the limit frame PHD2 holds.
    """

    def __init__(
        self,
        tmp_path,
        clock,
        *,
        size=(200, 200),
        star=(100.0, 100.0),
        lock=(100.0, 100.0),
        dates=None,
        limit=MATCHING,
        step_px=None,
    ):
        self.tmp_path, self.clock = tmp_path, clock
        self.data = _frame_data(size, star)
        self.lock = lock
        self.dates = dates or [f"2026-10-26T20:00:{i:02d}.000" for i in range(0, 60, 6)]
        if limit is MATCHING:
            from common.models.statuses import ImagerRoi

            limit = None if size == SENSOR else ImagerRoi.verbatim(x=1000, y=0, width=size[0], height=size[1])
        self.limit = limit
        self.step_px = step_px or (lambda i: 0.1)
        self.profile_binning = 1
        self.saved: list[str] = []
        self.moved: list[tuple[float, float]] = []

    @property
    def latest_guide_step(self) -> tuple[int, float]:
        n = int(self.clock.now // CADENCE_S)
        return n, self.step_px(n)

    def get_lock_position(self):
        return self.lock

    def get_limit_frame(self):
        return self.limit

    def set_lock_position(self, x, y):
        self.moved.append((x, y))

    def save_image(self) -> str:
        n = len(self.saved)
        path = str(self.tmp_path / f"guide-{n}.fits")
        hdu = fits.PrimaryHDU(self.data)
        hdu.header["DATE-OBS"] = self.dates[min(n, len(self.dates) - 1)]
        hdu.writeto(path)
        self.saved.append(path)
        return path


def _solution_off_target(dec_offset_arcsec: float) -> SolvingSolution:
    """A solve whose center sits `dec_offset_arcsec` south of the target at (15h, +30)."""
    sol = solution(dec_degs=30.0 - dec_offset_arcsec / 3600.0)
    sol.ra_hours = 15.0
    sol.dec_rads = math.radians(sol.dec_degs)
    return sol


class FakeSolver:
    """Solves each frame it is given, in order, `dec_offsets_arcsec[i]` off the target."""

    def __init__(self, dec_offsets_arcsec):
        self.offsets = list(dec_offsets_arcsec)
        self.calls: list[dict] = []

    def solve(self, **kwargs):
        from common.interfaces.solving import SolvingResult

        offset = self.offsets[min(len(self.calls), len(self.offsets) - 1)]
        self.calls.append(kwargs)
        return SolvingResult(succeeded=True, solution=_solution_off_target(offset))


TARGET_RA_H, TARGET_DEC = 15.0, 30.0


@pytest.fixture
def mastrometry_importable():
    try:
        import solvers.mastrometry  # noqa: F401 -- the nudge imports it for DOWNSAMPLE_FACTOR
    except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
        pytest.skip(f"solver import chain unavailable here ({ex!r})")


def _nudge(phd2: FakePHD2, solver: FakeSolver, is_current=lambda: True, **conf):
    from common.config.phd2 import LockNudgeConfig
    from common.utils import Coord
    from lock_nudge import nudge_lock_to_target

    unit = SimpleNamespace(solver=SimpleNamespace(_backend=solver))
    target = Coord(ra=Angle(TARGET_RA_H, unit="hour"), dec=Angle(TARGET_DEC, unit="deg"))
    settings = {"enabled": True, "confirm_frames": 0, **conf}
    return nudge_lock_to_target(unit, phd2, target, LockNudgeConfig(**settings), is_current=is_current)


@pytest.mark.usefixtures("mastrometry_importable")
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

    def test_the_lock_moves(self, tmp_path, clock):
        # one arcsecond of dec off: a few pixels, well inside max_offset_px
        phd2 = FakePHD2(tmp_path, clock)
        outcome = _nudge(phd2, FakeSolver([1.0]))
        assert outcome.applied, outcome.reason
        assert len(phd2.moved) == 1

    @pytest.mark.parametrize(
        ("size", "star"),
        [(SENSOR, (6102.4, 2561.7)), ((300, 200), (151.6, 98.3))],
        ids=["full-sensor", "limit-frame"],
    )
    def test_on_target_the_lock_goes_to_the_star_not_back_to_itself(self, tmp_path, clock, size, star):
        """Zero sky offset isolates the reference: the new lock is the star's position.

        The frame PHD2 saves is the frame it delivers -- the limit frame when one is in
        force -- so a position measured in it is in the lock's own coordinates.
        """
        from common.models.statuses import ImagerRoi

        lock = (round(star[0]) - 2.0, round(star[1]) + 1.0)
        limit = None if size == SENSOR else ImagerRoi.verbatim(x=520, y=0, width=size[0], height=size[1])
        phd2 = FakePHD2(tmp_path, clock, size=size, star=star, lock=lock, limit=limit)
        outcome = _nudge(phd2, FakeSolver([0.0]), solve_frames=1)
        assert outcome.applied, outcome.reason
        assert phd2.moved[0] == pytest.approx(star, abs=0.1)

    def test_no_star_near_the_lock_leaves_it_alone(self, tmp_path, clock):
        phd2 = FakePHD2(tmp_path, clock, star=(20.0, 20.0), lock=(150.0, 150.0))
        outcome = _nudge(phd2, FakeSolver([0.0]))
        assert not outcome.applied
        assert phd2.moved == []

    def test_a_star_too_far_from_the_lock_is_refused(self, tmp_path, clock):
        """A hot pixel, or a cosmic ray in a frame the star dropped out of, can sit anywhere
        in the search box; max_offset_px bounds the move the lock would make, not only D."""
        phd2 = FakePHD2(tmp_path, clock, star=(110.0, 108.0), lock=(100.0, 100.0))
        outcome = _nudge(phd2, FakeSolver([0.0]))
        assert not outcome.applied
        assert phd2.moved == []

    def test_every_saved_frame_is_removed(self, tmp_path, clock):
        phd2 = FakePHD2(tmp_path, clock)
        _nudge(phd2, FakeSolver([0.5]))
        assert phd2.saved and not list(tmp_path.glob("guide-*.fits"))


@pytest.mark.usefixtures("mastrometry_importable")
class TestTheMultiSolveGate:
    """One solve places the fiber to 0.56" radial (1 sigma), about the error being
    corrected, so the nudge solves several distinct guide frames and moves the lock
    to the mean of their estimates -- or not at all when they disagree."""

    def test_a_repeated_frame_counts_once(self, tmp_path, clock):
        # Asked faster than PHD2 delivers, save_image hands back the same frame again.
        dates = ["20:00:00", "20:00:00", "20:00:06", "20:00:06", "20:00:12", "20:00:18"]
        phd2 = FakePHD2(tmp_path, clock, dates=[f"2026-10-26T{d}.000" for d in dates])
        solver = FakeSolver([0.5])

        outcome = _nudge(phd2, solver, solve_frames=4)

        assert outcome.applied, outcome.reason
        assert outcome.frames_used == 4
        assert len(phd2.saved) == 6
        assert len(solver.calls) == 4

    def test_too_few_distinct_frames_by_the_timeout_is_no_nudge(self, tmp_path, clock):
        phd2 = FakePHD2(tmp_path, clock, dates=["2026-10-26T20:00:00.000"])

        outcome = _nudge(phd2, FakeSolver([0.5]), solve_frames=4, frames_timeout=90.0)

        assert not outcome.applied
        assert "1 of 4" in outcome.reason
        assert phd2.moved == []
        assert clock.now <= 90.0 + CADENCE_S

    def test_estimates_that_disagree_are_refused_and_the_lock_left_alone(self, tmp_path, clock):
        # +-2" about zero: an RMS radial spread of 1.41", over the 1.0" allowed
        phd2 = FakePHD2(tmp_path, clock)

        outcome = _nudge(phd2, FakeSolver([0.0, 2.0, -2.0, 0.0]), solve_frames=4, max_spread_arcsec=1.0)

        assert not outcome.applied
        assert outcome.spread_arcsec == pytest.approx(math.sqrt(2.0), abs=0.05)
        assert "spread" in outcome.reason
        assert phd2.moved == []

    def test_the_mean_estimate_is_applied(self, tmp_path, clock):
        uniform = FakePHD2(tmp_path / "a", clock)
        scattered = FakePHD2(tmp_path / "b", clock)
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()

        _nudge(uniform, FakeSolver([1.0]), solve_frames=4)
        outcome = _nudge(scattered, FakeSolver([0.8, 1.2, 0.9, 1.1]), solve_frames=4)

        assert outcome.applied, outcome.reason
        assert scattered.moved[0] == pytest.approx(uniform.moved[0], abs=1e-3)
        assert outcome.spread_arcsec == pytest.approx(math.sqrt(0.025), abs=0.01)

    def test_the_mean_is_still_bounded_by_max_offset_px(self, tmp_path, clock):
        # 2.5" of dec is 9.3 px of lock move, over the 7 px allowed, and the frames agree.
        phd2 = FakePHD2(tmp_path, clock)

        outcome = _nudge(phd2, FakeSolver([2.5]), solve_frames=4, max_spread_arcsec=1.0)

        assert not outcome.applied
        assert "max_offset_px" in outcome.reason
        assert phd2.moved == []

    def test_a_session_that_stops_mid_collection_is_not_nudged(self, tmp_path, clock):
        phd2 = FakePHD2(tmp_path, clock)
        solver = FakeSolver([0.5])

        outcome = _nudge(phd2, solver, is_current=lambda: len(solver.calls) < 2, solve_frames=4)

        assert not outcome.applied
        assert phd2.moved == []
        assert len(solver.calls) == 2

    def test_a_session_that_stops_during_the_last_solve_is_not_nudged(self, tmp_path, clock):
        phd2 = FakePHD2(tmp_path, clock)
        solver = FakeSolver([0.5])

        outcome = _nudge(phd2, solver, is_current=lambda: len(solver.calls) < 4, solve_frames=4)

        assert not outcome.applied
        assert phd2.moved == []
        assert len(solver.calls) == 4

    def test_a_cropped_guide_frame_is_solved_with_its_origin(self, tmp_path, clock):
        """mastrometry places the sensor-pixel ROI on the frame it is given; on a crop
        without `frame_origin` it raises, and every nudge on a limit frame failed."""
        from common.models.statuses import ImagerRoi

        limit = ImagerRoi.verbatim(x=2972, y=0, width=300, height=200)
        phd2 = FakePHD2(tmp_path, clock, size=(300, 200), star=(150.0, 100.0), lock=(150.0, 100.0), limit=limit)
        solver = FakeSolver([0.5])

        outcome = _nudge(phd2, solver, solve_frames=2)

        assert outcome.applied, outcome.reason
        assert [c["frame_origin"] for c in solver.calls] == [(2972, 0), (2972, 0)]

    def test_a_full_sensor_frame_is_solved_without_an_origin(self, tmp_path, clock):
        phd2 = FakePHD2(tmp_path, clock, size=SENSOR, star=(6102.4, 2561.7), lock=(6102.0, 2562.0))
        solver = FakeSolver([0.5])

        _nudge(phd2, solver, solve_frames=1)

        assert "frame_origin" not in solver.calls[0]

    def test_a_frame_that_matches_neither_sensor_nor_limit_frame_is_refused(self, tmp_path, clock):
        phd2 = FakePHD2(tmp_path, clock, size=(300, 200), star=(150.0, 100.0), lock=(150.0, 100.0), limit=None)
        solver = FakeSolver([0.5])

        outcome = _nudge(phd2, solver)

        assert not outcome.applied
        assert solver.calls == []
        assert phd2.moved == []


class TestConfirmation:
    """`set_lock_position` starts no settle, so the move is confirmed by counting guide
    frames under `confirm_px`. A GuideStep arrives every 4.6-9.1 s; counting 1 s polls
    instead confirmed about 2 s after the move, on the old lock's distance.

    The first step after the move is from the frame that was exposing during it -- the
    unmoved star against the new lock -- so it does not count either."""

    def test_it_counts_guide_frames_not_polls(self, clock, monkeypatch):
        from common.config.phd2 import LockNudgeConfig
        from lock_nudge import _await_confirmation

        # step 1 is the frame exposing during the move; steps 2 and 3 are on the new lock
        distances = {0: 3.0, 1: 0.8, 2: 0.4, 3: 0.3}
        phd2 = SimpleNamespace(latest_guide_step=(0, 3.0))

        def sleep(seconds):
            clock.now += seconds
            n = int(clock.now // CADENCE_S)
            phd2.latest_guide_step = (n, distances.get(n, 0.3))

        import lock_nudge

        monkeypatch.setattr(lock_nudge.time, "sleep", sleep)
        residual = _await_confirmation(phd2, LockNudgeConfig(confirm_frames=2, confirm_px=1.0, confirm_timeout=60.0))

        assert clock.now >= 3 * CADENCE_S
        assert residual == pytest.approx(0.3)

    def test_a_step_over_confirm_px_restarts_the_count(self, clock, monkeypatch):
        from common.config.phd2 import LockNudgeConfig
        from lock_nudge import _await_confirmation

        distances = {1: 0.5, 2: 0.4, 3: 2.0, 4: 0.3, 5: 0.2}
        phd2 = SimpleNamespace(latest_guide_step=(0, 3.0))

        def sleep(seconds):
            clock.now += seconds
            n = int(clock.now // CADENCE_S)
            phd2.latest_guide_step = (n, distances.get(n, 0.2))

        import lock_nudge

        monkeypatch.setattr(lock_nudge.time, "sleep", sleep)
        residual = _await_confirmation(phd2, LockNudgeConfig(confirm_frames=2, confirm_px=1.0, confirm_timeout=60.0))

        assert clock.now >= 5 * CADENCE_S
        assert residual == pytest.approx(0.2)


class TestTheOutcomeIsPublished:
    """An operator sees a refused nudge in `status.latest.acquisition.nudge`, without the log."""

    def test_a_refusal_and_its_reason_reach_the_acquisition_outcome(self, monkeypatch):
        import phd2.phd2 as phd2_module
        from common.config.phd2 import LockNudgeConfig
        from common.models.latest import AcquisitionState, LatestAcquisition
        from lock_nudge import NudgeOutcome
        from phd2.phd2 import PHD2Connector

        refusal = NudgeOutcome(
            applied=False, reason='spread 1.41" exceeds max_spread_arcsec 1.0', spread_arcsec=1.41, frames_used=4
        )
        monkeypatch.setattr(phd2_module, "nudge_lock_to_target", lambda *args, **kwargs: refusal)
        monkeypatch.setattr(
            PHD2Connector, "conf", property(lambda self: SimpleNamespace(lock_nudge=LockNudgeConfig(enabled=True)))
        )
        latest = LatestAcquisition(state=AcquisitionState.RUNNING).begin()
        acquisition = SimpleNamespace(target_ra=TARGET_RA_H, target_dec=TARGET_DEC, latest=latest)
        connector = object.__new__(PHD2Connector)
        connector._nudge_onto_target = True
        connector.parent = SimpleNamespace(unit=SimpleNamespace(acquirer=SimpleNamespace(latest_acquisition=acquisition)))

        connector._nudge_lock_onto_target(session=1)

        assert latest.nudge is not None
        assert latest.nudge.applied is False
        assert latest.nudge.reason == refusal.reason
        assert (latest.nudge.spread_arcsec, latest.nudge.frames_used) == (1.41, 4)


def test_the_connector_counts_guide_steps_with_their_own_distance():
    """What the gate and the confirmation wait on: one count per GuideStep, with that
    step's own guide distance -- not the smoothed `AvgDist`, which lags a lock move."""
    import threading

    from phd2.phd2 import PHD2Connector

    c = object.__new__(PHD2Connector)
    c.lock = threading.Lock()
    c.accumulators_active = False
    c.latest_guide_step = (0, 0.0)
    c.start_activity = lambda *a, **kw: None
    c.sky_quality = SimpleNamespace(update=lambda metrics: None)
    c._update_lock_validity = lambda ev: None
    step = {"Event": "GuideStep", "Frame": 7, "Time": 40.0, "RADistanceRaw": 0.0, "DECDistanceRaw": 0.0}
    step |= {"SNR": 50.0, "HFD": 2.5, "StarMass": 1e5}

    c._handle_event({**step, "dx": 3.0, "dy": 4.0, "AvgDist": 1.0})
    c._handle_event({**step, "dx": 0.3, "dy": 0.4, "AvgDist": 0.9})

    assert c.latest_guide_step == (2, pytest.approx(0.5))
