"""An acquisition does not expose until the slew to its target has settled.

The only slew wait used to sit in the FCU v1 `sky` phase, so on FCU v2 -- every production
unit -- and on v1 with `skip_sky`, the first frame was taken mid-slew (MAST_unit#315).

Needs no hardware: every component is a stub that records what it was asked to do.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import acquirer as acquirer_module
from acquirer import ACQUISITION_CANCELLED, Acquirer
from common.activities import UnitActivities
from common.config.rois import FcuVersion, SkyRoiConfig
from mount import SETTLE_START_GRACE_SECONDS, SLEW_SETTLE_TOLERANCE_ARCSEC

SLEW_WAIT = "slew-wait"
SOLVE = "solve"


class _Mount:
    def __init__(self, events, unit, cancel_during_slew=False):
        self.events, self.unit, self.cancel_during_slew = events, unit, cancel_during_slew
        self.settles: list[dict] = []

    def start_tracking(self):
        pass

    def stop_tracking(self):
        pass

    def goto_ra_dec_j2000(self, ra, dec):
        self.events.append("goto")

    def wait_until_settled(self, mode, **kwargs):
        self.events.append(SLEW_WAIT)
        self.settles.append(kwargs)
        if self.cancel_during_slew:
            self.unit.activities.discard(UnitActivities.Acquiring)
        return True


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class _Stage:
    is_moving = False
    position = 0

    def __init__(self, events, clock, travel_seconds):
        self.events, self.clock, self.travel_seconds = events, clock, travel_seconds

    def move_to_preset(self, preset):
        self.events.append(f"stage->{preset}")
        self.clock.now += self.travel_seconds


class _Imager:
    connected = True

    def start_exposure_series(self, purpose):
        return "series"

    def end_exposure_series(self, series):
        pass


class _Solver:
    def __init__(self, events):
        self.events = events

    def solve_and_correct(self, **kwargs):
        self.events.append(SOLVE)
        return False


class _Guider:
    def make_guiding_settings(self, base_folder):
        return SimpleNamespace()


_SKY_ROI = SkyRoiConfig(sky_x=4000, sky_y=2800, width=1024, height=1024)


class _Unit:
    def __init__(self, fcu_version, cancel_during_slew=False, stage_travel_seconds=0.0):
        self.events: list[str] = []
        self.activities: set = set()
        self.errors: list[str] = []
        self.reference_image = None
        self.fcu_version = fcu_version
        tolerance = SimpleNamespace(ra_arcsec=1.0, dec_arcsec=1.0)
        self.unit_conf = SimpleNamespace(
            acquisition=SimpleNamespace(
                tries=1, exposure=1.0, binning=1, gain=None, rois={fcu_version: _SKY_ROI}, tolerance=tolerance
            ),
            guiding=SimpleNamespace(tolerance=tolerance),
        )
        self.mount = self.required_mount = _Mount(self.events, self, cancel_during_slew)
        self.clock = _Clock()
        self.stage = self.required_stage = _Stage(self.events, self.clock, stage_travel_seconds)
        self.imager = self.required_imager = _Imager()
        self.solver = self.required_solver = _Solver(self.events)
        self.guider = self.required_guider = _Guider()

    def start_activity(self, activity):
        self.activities.add(activity)

    def end_activity(self, activity):
        self.activities.discard(activity)

    def is_active(self, activity):
        return activity in self.activities


def _acquisition(slew_to_target=True, skip_sky=False):
    return SimpleNamespace(
        target_ra=1.0,
        target_dec=10.0,
        slew_to_target=slew_to_target,
        skip_sky=skip_sky,
        exposure=None,
        folder="unused",
        approach_mode=None,
        solver_id=None,
        make_corrections=True,
        use_set_limit_frame=False,
        gain_absolute=None,
        gain_percent=None,
        save_corrections=lambda phase: None,
    )


def _acquire(unit, acquisition, monkeypatch):
    a = object.__new__(Acquirer)
    a.unit = unit
    monkeypatch.setattr(a, "_cancellable_sleep", lambda seconds: not a._cancelled())
    monkeypatch.setattr(acquirer_module, "time", SimpleNamespace(monotonic=unit.clock.monotonic, sleep=lambda s: None))
    return Acquirer.do_acquire(a, acquisition)


PATHS = pytest.mark.parametrize(
    ("fcu_version", "skip_sky"),
    [(FcuVersion.v2, False), (FcuVersion.v1, True), (FcuVersion.v1, False)],
    ids=["fcu-v2", "fcu-v1-skip-sky", "fcu-v1-sky-phase"],
)


@PATHS
def test_the_first_exposure_waits_for_the_slew(fcu_version, skip_sky, monkeypatch):
    unit = _Unit(fcu_version)

    _acquire(unit, _acquisition(skip_sky=skip_sky), monkeypatch)

    assert SLEW_WAIT in unit.events
    assert unit.events.index(SLEW_WAIT) < unit.events.index(SOLVE)


def test_the_slew_settles_on_arrival_not_on_quiet_tracking(monkeypatch):
    unit = _Unit(FcuVersion.v2)

    _acquire(unit, _acquisition(), monkeypatch)

    assert unit.mount.settles[0]["dist_tolerance_arcsec"] == SLEW_SETTLE_TOLERANCE_ARCSEC


@pytest.mark.parametrize(
    ("stage_travel_seconds", "expected_grace"),
    [(0.0, SETTLE_START_GRACE_SECONDS), (1.0, SETTLE_START_GRACE_SECONDS - 1.0), (40.0, 0.0)],
    ids=["no-travel", "short-travel", "grace-spent"],
)
def test_only_the_unspent_start_grace_is_passed_on(stage_travel_seconds, expected_grace, monkeypatch):
    unit = _Unit(FcuVersion.v2, stage_travel_seconds=stage_travel_seconds)

    _acquire(unit, _acquisition(), monkeypatch)

    assert unit.mount.settles[0]["start_grace_seconds"] == pytest.approx(expected_grace)


def test_no_slew_wait_when_there_is_no_slew(monkeypatch):
    unit = _Unit(FcuVersion.v2)

    _acquire(unit, _acquisition(slew_to_target=False), monkeypatch)

    assert SLEW_WAIT not in unit.events
    assert SOLVE in unit.events


@PATHS
def test_a_cancel_during_the_slew_wait_takes_no_exposure(fcu_version, skip_sky, monkeypatch):
    unit = _Unit(fcu_version, cancel_during_slew=True)

    failure = _acquire(unit, _acquisition(skip_sky=skip_sky), monkeypatch)

    assert failure == ACQUISITION_CANCELLED
    assert SOLVE not in unit.events
    assert unit.activities == set()
