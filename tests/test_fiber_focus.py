"""The fiber-focus run, driven end to end without hardware.

What is checked here is that the run waits for THIS guiding session's `SettleDone` and no
other signal, fails cleanly when the settle fails or never comes, never reads PHD2 once it
has settled, steps the focuser through exactly the requested positions, names each frame by
the MEASURED position, and always sends the focuser back to `known_as_good_position`.

The mount, stage, acquirer, focuser and PHD2 are fakes; the flux meter is the simulator the
production code accepts by injection.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from astropy.io import fits

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import flux_metering.fiber_focus as fiber_module
from common.activities import FocuserActivities, UnitActivities
from flux_metering.fiber_focus import FiberFocusParams, FiberFocusSession, frame_name
from flux_metering.flux_meter import SimulatedFluxMeter
from phd2.phd2 import PHD2SettleProgress


class FakeFocuser:
    """Arrives after a few polls, `offset` steps away from where it was sent -- inside
    `CLOSE_ENOUGH`, as the real one may."""

    CLOSE_ENOUGH = 2

    def __init__(self, offset: int = 1, polls_to_arrive: int = 2):
        self.lower_limit = 0
        self.upper_limit = 30000
        self.known_as_good_position = 15000
        self.offset = offset
        self.polls_to_arrive = polls_to_arrive
        self._position = 12345
        self._remaining = 0
        self.target = None
        self.gotos: list[int] = []
        self.aborting = False

    def goto_position(self, value):
        self.gotos.append(value)
        self.target = value
        self._remaining = self.polls_to_arrive
        return SimpleNamespace(failed=False, errors=None)

    def is_active(self, flag):
        if flag == FocuserActivities.Aborting:
            return self.aborting
        if flag != FocuserActivities.Moving or self.target is None:
            return False
        if self._remaining > 0:
            self._remaining -= 1
            return True
        self._position = self.target + self.offset
        self.target = None
        return False

    @property
    def position(self):
        return self._position

    def close_enough(self, value):
        return abs(self._position - value) <= self.CLOSE_ENOUGH


class FakePHD2:
    """The connector's settle record, written by a pretend event thread.

    `guide()` replaces the record with a fresh not-done one, as the real one does; the
    `SettleDone` then arrives after `settle_after_s`, with `settle_status`. `settle_reads`
    counts every read of the record, so a test can prove the run stops reading it.
    """

    def __init__(self, settle_status: int | None = 0, settle_after_s: float = 0.02, error: str | None = None):
        self.lock = threading.Lock()
        stale = PHD2SettleProgress()
        stale.done, stale.status = True, 0  # an EARLIER session's success, left behind
        self._settle = stale
        self.settle_status = settle_status
        self.settle_after_s = settle_after_s
        self.error = error
        self.guides = 0
        self.settle_reads = 0
        self.unit = None

    @property
    def settle(self):
        self.settle_reads += 1
        return self._settle

    def start_guiding(self):
        self.unit.start_activity(UnitActivities.Guiding)
        self.guides += 1
        with self.lock:
            self._settle = PHD2SettleProgress()
        if self.settle_status is not None:
            threading.Timer(self.settle_after_s, self._settle_done).start()
        return SimpleNamespace(failed=False, errors=None)

    def _settle_done(self):
        s = PHD2SettleProgress()
        s.done, s.status, s.error = True, self.settle_status, self.error
        with self.lock:
            self._settle = s


class FakeMount:
    def __init__(self):
        self.is_tracking = True

    def start_tracking(self):
        self.is_tracking = True

    def status(self):
        return SimpleNamespace(dec_j2000_degs=41.0, ra_j2000_hours=13.5, ha_hours=0.25, lmst_hours=14.0)


class FakeAcquirer:
    def __init__(self, failure=None):
        self.failure = failure
        self.requests: list[dict] = []

    def prepare_acquisition(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(failure=None)

    def run_acquisition(self, acquisition):
        acquisition.failure = self.failure


class FakeUnit:
    def __init__(self, focuser, phd2, acquirer=None):
        self.mount = FakeMount()
        self.imager = object()
        self.focuser = focuser
        self.acquirer = acquirer or FakeAcquirer()
        self.stage = SimpleNamespace(at_preset=lambda preset: True, position=283350)
        self.guider = object()
        self.hostname = "test-unit"
        self.activities = UnitActivities(0)
        self.activities_verbal: list = []
        self.was_tracking_before_guiding = None
        phd2.unit = self

    def is_active(self, flag):
        return bool(self.activities & flag)

    def start_activity(self, flag, **kwargs):
        self.activities |= flag

    def end_activity(self, flag, **kwargs):
        self.activities &= ~flag


def _params(**overrides) -> FiberFocusParams:
    values = dict(
        ra_j2000_hours=13.5,
        dec_j2000_degs=41.0,
        start=14900,
        step=50,
        count=4,
        flux_seconds=0.001,
        settle_seconds=0.0,
        guide_settle_timeout_seconds=2.0,
    )
    values.update(overrides)
    return FiberFocusParams(**values)


@pytest.fixture
def make(tmp_path, monkeypatch):
    """Build a session on fakes. The mover and the folder maker are stubbed: the frames
    land in `tmp_path`, under no configured root."""
    monkeypatch.setattr(fiber_module, "POLL_SECONDS", 0.001)
    monkeypatch.setattr(fiber_module.filer, "move_ram_to_shared", lambda *a, **k: None)
    monkeypatch.setattr(fiber_module.filer, "flush", lambda **kw: True)
    folder = tmp_path / "2026-10-19" / "FiberFocus" / "0001"
    folder.mkdir(parents=True)
    monkeypatch.setattr(fiber_module, "PathMaker", lambda: SimpleNamespace(make_fiber_focus_folder=lambda: str(folder)))

    def build(phd2=None, focuser=None, acquirer=None):
        phd2 = phd2 or FakePHD2()
        focuser = focuser or FakeFocuser()
        unit = FakeUnit(focuser, phd2, acquirer)
        monkeypatch.setattr(fiber_module, "phd2_backend", lambda u: phd2)
        session = FiberFocusSession(unit, flux_meter=SimulatedFluxMeter())  # type: ignore[arg-type]
        return session, unit, focuser, phd2, folder

    return build


def _run(session, params):
    assert session.start(params) is not None
    session._thread.join(timeout=10)
    assert not session.is_active, "the run did not end"
    return session.status()


def test_a_run_steps_through_every_position_and_returns_the_focuser(make):
    s, unit, focuser, phd2, folder = make()

    status = _run(s, _params())

    assert status.terminal_state == "completed"
    assert [p.commanded for p in status.positions] == [14900, 14950, 15000, 15050]
    assert [p.measured for p in status.positions] == [14901, 14951, 15001, 15051]
    assert focuser.gotos == [14900, 14950, 15000, 15050, 15000], "not returned to known_as_good_position"
    assert status.final_focuser_commanded == 15000
    assert phd2.guides == 1
    assert status.guide_settle.outcome == "settled"
    assert not unit.is_active(UnitActivities.FluxMetering)
    assert unit.is_active(UnitActivities.Guiding), "guiding must be left running"


def test_frames_are_named_and_stamped_with_the_measured_position(make):
    s, _unit, _focuser, _phd2, folder = make()

    status = _run(s, _params(count=2, frames_per_position=2))

    names = [f.name for p in status.positions for f in p.frames]
    assert names == [
        "flux-001-14901-00.fits",
        "flux-001-14901-01.fits",
        "flux-002-14951-00.fits",
        "flux-002-14951-01.fits",
    ]
    header = fits.getheader(folder / "flux-002-14951-01.fits")
    assert header["FOCUSPOS"] == 14951
    assert header["FOCUSCMD"] == 14950
    assert header["POSIDX"] == 2
    assert header["FRAMEIDX"] == 1
    assert header["RUNDATE"] == "2026-10-19"
    assert header["RUNSEQ"] == "0001"


def test_the_run_waits_for_this_sessions_settle_not_an_earlier_one(make):
    """The connector still holds an earlier session's successful SettleDone. If the run
    read that, it would start the sweep at once; it must wait for the new one."""
    phd2 = FakePHD2(settle_after_s=0.3)
    s, _unit, _focuser, _phd2, _folder = make(phd2=phd2)

    status = _run(s, _params(count=1))

    assert status.terminal_state == "completed"
    assert status.guide_settle.waited_seconds >= 0.3


def test_phd2_is_not_read_once_guiding_has_settled(make):
    phd2 = FakePHD2()
    s, _unit, _focuser, _phd2, _folder = make(phd2=phd2)
    reads_at_settle = []
    original = s._sweep

    def sweep():
        reads_at_settle.append(phd2.settle_reads)
        return original()

    s._sweep = sweep  # type: ignore[method-assign]

    _run(s, _params())

    assert phd2.settle_reads == reads_at_settle[0]


def test_a_failed_settle_fails_the_run_before_the_focuser_moves(make):
    phd2 = FakePHD2(settle_status=1, error="star lost")
    s, unit, focuser, _phd2, folder = make(phd2=phd2)

    status = _run(s, _params())

    assert status.terminal_state == "guiding_failed"
    assert status.guide_settle.outcome == "failed"
    assert "star lost" in status.last_error
    assert focuser.gotos == [15000], "only the return to known_as_good_position"
    assert status.positions == []
    result = json.loads((folder / "result.json").read_text())
    assert result["terminal_state"] == "guiding_failed"
    assert "star lost" in result["last_error"]


def test_no_settle_done_in_time_fails_the_run_with_its_own_reason(make):
    phd2 = FakePHD2(settle_status=None)  # SettleDone never arrives
    s, _unit, _focuser, _phd2, _folder = make(phd2=phd2)

    status = _run(s, _params(guide_settle_timeout_seconds=0.1))

    assert status.terminal_state == "guiding_failed"
    assert status.guide_settle.outcome == "timeout"
    assert status.last_error == "no SettleDone from PHD2 within 0.1 s"


def test_a_failed_acquisition_never_starts_guiding(make):
    s, _unit, focuser, phd2, _folder = make(acquirer=FakeAcquirer(failure="no solve"))

    status = _run(s, _params())

    assert status.terminal_state == "acquisition_failed"
    assert phd2.guides == 0
    assert focuser.gotos == [15000]


def test_the_acquisition_does_not_hand_over_to_the_guider(make):
    s, unit, _focuser, _phd2, _folder = make()

    _run(s, _params(count=1))

    assert unit.acquirer.requests[0]["handover_automatically_to_guider"] is False
    assert unit.acquirer.requests[0]["ra_j2000_hours"] == 13.5


def test_an_abort_mid_sweep_still_returns_the_focuser(make):
    s, _unit, focuser, _phd2, folder = make()
    original = s._expose

    def expose_then_abort(position, n):
        frame = original(position, n)
        if position.index == 1:
            s.abort()
        return frame

    s._expose = expose_then_abort  # type: ignore[method-assign]

    status = _run(s, _params(count=5))

    assert status.terminal_state == "aborted"
    assert len(status.positions) == 2
    assert focuser.gotos[-1] == 15000
    assert json.loads((folder / "result.json").read_text())["terminal_state"] == "aborted"


def test_result_json_records_what_was_asked_and_what_happened(make):
    s, _unit, _focuser, _phd2, folder = make()

    _run(s, _params(count=2))

    result = json.loads((folder / "result.json").read_text())
    assert result["active"] is False
    assert result["params"]["step"] == 50
    assert result["commanded_positions"] == [14900, 14950]
    assert [p["measured"] for p in result["positions"]] == [14901, 14951]
    assert result["guide_settle"]["outcome"] == "settled"
    assert result["focuser_limits"] == [0, 30000]
    assert result["final_focuser_commanded"] == 15000


@pytest.mark.parametrize(
    ("activity", "fragment"),
    [
        (UnitActivities.Guiding, "busy"),
        (UnitActivities.FluxMetering, "busy"),
        (UnitActivities.Autofocusing, "busy"),
        (UnitActivities.Acquiring, "busy"),
    ],
)
def test_it_refuses_while_the_unit_is_busy(make, activity, fragment):
    s, unit, focuser, _phd2, _folder = make()
    unit.activities |= activity

    refusal = s.start(_params())

    assert isinstance(refusal, str) and fragment in refusal
    assert focuser.gotos == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        (dict(start=29950, step=50, count=2), "outside"),
        (dict(start=50, step=-100, count=2), "outside"),
        (dict(step=0, count=3), "step of 0"),
    ],
)
def test_it_refuses_positions_it_cannot_sweep(make, overrides, fragment):
    s, _unit, focuser, _phd2, _folder = make()

    refusal = s.start(_params(**overrides))

    assert isinstance(refusal, str) and fragment in refusal
    assert focuser.gotos == []


def test_it_refuses_a_guider_that_is_not_phd2(make, monkeypatch):
    s, _unit, _focuser, _phd2, _folder = make()
    monkeypatch.setattr(fiber_module, "phd2_backend", lambda u: None)

    refusal = s.start(_params())

    assert isinstance(refusal, str) and "not PHD2" in refusal


def test_frame_name_shape():
    assert frame_name(0, 15001, 0) == "flux-001-15001-00.fits"
    assert frame_name(998, 7, 12) == "flux-999-00007-12.fits"


# ----------------------------------------------------------------- the endpoint --


def _endpoint_runner(known_as_good=15000):
    started: list = []
    runner = SimpleNamespace(
        focuser=SimpleNamespace(known_as_good_position=known_as_good),
        fiber_focus=SimpleNamespace(start=lambda params: started.append(params) or SimpleNamespace()),
    )
    return runner, started


def test_the_endpoint_defaults_start_to_known_as_good_position():
    from unit import Unit

    runner, started = _endpoint_runner()

    response = Unit.endpoint_acquire_and_focus_fiber(runner, "13:30:00", "+41:00:00", step=25, count=3)  # type: ignore[arg-type]

    assert not response.errors
    assert started[0].start == 15000
    assert started[0].positions == [15000, 15025, 15050]
    assert started[0].ra_j2000_hours == pytest.approx(13.5)


def test_the_endpoint_refuses_without_start_or_known_as_good_position():
    from unit import Unit

    runner, started = _endpoint_runner(known_as_good=None)

    response = Unit.endpoint_acquire_and_focus_fiber(runner, 13.5, 41.0, step=25, count=3)  # type: ignore[arg-type]

    assert response.errors and "known_as_good_position" in response.errors[0]
    assert started == []


def test_both_fiber_routes_exist():
    import inspect

    from unit import Unit

    source = inspect.getsource(Unit)
    for route in ("acquire_and_focus_fiber", "focus_fiber_status"):
        assert f'base_path + "/{route}"' in source, route
