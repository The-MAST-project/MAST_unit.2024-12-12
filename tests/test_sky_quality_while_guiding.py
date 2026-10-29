"""The sky's quality while guiding: what PHD2's guide frames say, per guiding session.

`status.latest.sky_quality.while_guiding` (common.models.latest.LatestGuidingSkyQuality).
The estimator it replaces scored frames against their own recent history -- steady bad seeing
and steady good seeing both read ~50 -- was never reset between sessions, assumed a fixed
plate scale, and read `SNR`/`HFD` unguarded on PHD2's reader thread, the thread that also
delivers every RPC reply.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from common.activities import Activities
from common.notifications import Notifier
from phd2.phd2 import PHD2Accumulator, PHD2Activities, PHD2Connector
from science.sky_quality import (
    BASELINE_SECONDS,
    MIN_BASELINE_SAMPLES,
    SUMMARY_SECONDS,
    WINDOWS_SECONDS,
    GuidingSkyQuality,
)

PLATE_SCALE = 0.26


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def step(hfd=4.0, snr=40.0, mass=10000.0, **extra) -> dict:
    return {"Event": "GuideStep", "HFD": hfd, "SNR": snr, "StarMass": mass, **extra}


def started(clock: Clock) -> GuidingSkyQuality:
    sq = GuidingSkyQuality(clock=clock)
    sq.start(plate_scale_arcsec=PLATE_SCALE, binning=1, plan_id="PLAN_X")
    return sq


def window(sq: GuidingSkyQuality, seconds: int):
    record = sq.latest
    assert record is not None
    return next(w for w in record.windows if w.seconds == seconds)


class TestTheEstimator:
    def test_nothing_before_the_first_session(self):
        assert GuidingSkyQuality().latest is None

    def test_a_session_reports_seeing_in_arcseconds(self):
        clock = Clock()
        sq = started(clock)
        for hfd in (4.0, 5.0, 6.0):
            sq.guide_step(step(hfd=hfd))
            clock.now += 2

        w = window(sq, 60)
        assert w.samples == 3
        assert w.hfd_px == 5.0
        assert w.hfd_arcsec == pytest.approx(5.0 * PLATE_SCALE)
        assert w.span_seconds == 4
        record = sq.latest
        assert record.running and record.plan_id == "PLAN_X" and record.samples == 3

    def test_without_a_plate_scale_the_hfd_stays_in_pixels(self):
        sq = GuidingSkyQuality(clock=Clock())
        sq.start(plate_scale_arcsec=None, binning=1)
        sq.guide_step(step(hfd=4.0))
        w = window(sq, 60)
        assert w.hfd_px == 4.0 and w.hfd_arcsec is None

    def test_each_window_holds_only_its_own_span(self):
        clock = Clock()
        sq = started(clock)
        sq.guide_step(step(hfd=10.0))  # will be 200 s old
        clock.now += 200
        sq.guide_step(step(hfd=2.0))

        assert [w.seconds for w in sq.latest.windows] == list(WINDOWS_SECONDS)
        assert window(sq, 60).samples == 1 and window(sq, 60).hfd_px == 2.0
        assert window(sq, 180).samples == 1
        assert window(sq, 300).samples == 2 and window(sq, 300).hfd_px == 6.0

    def test_frames_without_a_usable_hfd_or_flagged_by_phd2_are_rejected_not_fatal(self):
        sq = started(Clock())
        for ev in (
            {"Event": "GuideStep"},  # no HFD, no SNR
            step(hfd=0),
            step(hfd="4"),
            step(hfd=float("nan")),
            step(ErrorCode=1),  # saturated
        ):
            sq.guide_step(ev)

        record = sq.latest
        assert record.samples == 0 and record.rejected == 5
        assert window(sq, 60).rejected == 5 and window(sq, 60).hfd_px is None

    def test_transparency_is_relative_to_the_sessions_first_minute(self):
        clock = Clock()
        sq = started(clock)
        for _ in range(BASELINE_SECONDS // 2 + 1):
            sq.guide_step(step(mass=10000.0))
            clock.now += 2
        assert sq.latest.baseline_star_mass == 10000.0

        for _ in range(40):  # a cloud: half the light, for over a minute
            sq.guide_step(step(mass=5000.0))
            clock.now += 2
        assert window(sq, 60).transparency == pytest.approx(0.5)

    def test_no_baseline_from_too_few_frames(self):
        clock = Clock()
        sq = started(clock)
        for _ in range(MIN_BASELINE_SAMPLES - 1):
            sq.guide_step(step())
            clock.now += BASELINE_SECONDS
        assert sq.latest.baseline_star_mass is None
        assert window(sq, 60).transparency is None

    def test_star_lost_is_counted(self):
        sq = started(Clock())
        sq.star_lost()
        sq.star_lost()
        assert sq.latest.star_lost == 2 and window(sq, 60).star_lost == 2

    def test_a_new_session_starts_empty(self):
        clock = Clock()
        sq = started(clock)
        sq.guide_step(step(hfd=9.0))
        sq.star_lost()
        sq.stop()
        sq.start(plate_scale_arcsec=PLATE_SCALE, binning=2)

        record = sq.latest
        assert record.running and record.samples == 0 and record.star_lost == 0
        assert record.binning == 2 and window(sq, 300).samples == 0

    def test_frames_outside_a_session_are_ignored(self):
        sq = started(Clock())
        sq.stop()
        sq.guide_step(step())
        sq.star_lost()
        record = sq.latest
        assert record.succeeded is True and record.samples == 0 and record.star_lost == 0

    def test_a_lost_connection_ends_the_session_as_failed(self):
        sq = started(Clock())
        sq.stop("the connection to PHD2 was lost")
        sq.stop()  # idempotent
        record = sq.latest
        assert record.succeeded is False and record.errors == ["the connection to PHD2 was lost"]

    def test_status_reads_a_copy(self):
        sq = started(Clock())
        copy = sq.latest
        sq.guide_step(step())
        assert copy.samples == 0 and sq.latest.samples == 1


class TestTheSummary:
    """A boxed INFO summary once a minute while guiding, and once more when the session ends."""

    @staticmethod
    def boxes(caplog) -> list[str]:
        return [r.getMessage() for r in caplog.records if "sky quality while guiding" in r.getMessage()]

    def test_none_before_a_minute_then_one_per_minute(self, caplog):
        clock = Clock()
        sq = started(clock)
        with caplog.at_level("INFO"):
            for _ in range(SUMMARY_SECONDS // 2 - 1):  # 58 s of frames
                sq.guide_step(step())
                clock.now += 2
            assert self.boxes(caplog) == []
            for _ in range(SUMMARY_SECONDS // 2):  # past a minute, short of two
                sq.guide_step(step())
                clock.now += 2
        assert len(self.boxes(caplog)) == 1

    def test_the_summary_says_what_each_window_measured(self, caplog):
        clock = Clock()
        sq = started(clock)
        with caplog.at_level("INFO"):
            for _ in range(SUMMARY_SECONDS // 2 + 1):
                sq.guide_step(step(hfd=4.0, snr=40.0))
                clock.now += 2
        (box,) = self.boxes(caplog)
        assert "plan PLAN_X" in box
        assert '1m: HFD 1.04" +/- 0.00 (4.00 px), SNR 40' in box
        assert "3m:" in box and "5m:" in box
        assert box.isascii()

    def test_the_end_of_a_session_is_summarised(self, caplog):
        sq = started(Clock())
        with caplog.at_level("INFO"):
            sq.stop("the connection to PHD2 was lost")
        (box,) = self.boxes(caplog)
        assert "(session ended)" in box and "errors: the connection to PHD2 was lost" in box
        assert "1m: no frames" in box


def connector(binning: int = 2) -> PHD2Connector:
    """A bare connector with just what its event handler touches -- __init__ reaches hardware."""
    c = object.__new__(PHD2Connector)
    Activities.__init__(c)
    unit_conf = SimpleNamespace(
        imager=SimpleNamespace(pixel_scale_at_bin1=PLATE_SCALE),
        phd2=SimpleNamespace(profile=f"MAST, binning={binning}, bpp=16"),
    )
    c.parent = SimpleNamespace(unit=SimpleNamespace(unit_conf=unit_conf, plan_owner="PLAN_Y"))
    c.sky_quality = GuidingSkyQuality(clock=Clock())
    c.guiding_verification_timer = None
    c.accumulators_active = False
    c.ra_accumulator = PHD2Accumulator()
    c.dec_accumulator = PHD2Accumulator()
    c.app_state = "Stopped"
    c.avg_dist = 0.0
    c.stats = None
    c.settle = None
    return c


def guide_step(**overrides) -> dict:
    ev = {"Frame": 1, "Time": 1.0, "dx": 0.1, "dy": 0.1, "RADistanceRaw": 0.1, "DECDistanceRaw": 0.1, "AvgDist": 0.1}
    return {**ev, **step(), **overrides}


@pytest.fixture
def quiet_notifier(monkeypatch):
    """`start_activity` notifies the GUI; the real Notifier reads this machine's config file."""
    monkeypatch.setattr(Notifier, "_instance", object.__new__(Notifier))
    monkeypatch.setattr(Notifier, "_initialized", True)
    monkeypatch.setattr(Notifier, "ui_notification", lambda *a, **k: None)


@pytest.mark.usefixtures("quiet_notifier")
class TestTheConnector:
    def test_start_guiding_opens_a_session_at_the_guiding_plate_scale(self):
        c = connector(binning=2)
        c._handle_event({"Event": "StartGuiding"})
        c._handle_event(guide_step(HFD=3.0))

        record = c.sky_quality_while_guiding
        assert record.running and record.binning == 2 and record.plan_id == "PLAN_Y"
        assert record.plate_scale_arcsec == pytest.approx(2 * PLATE_SCALE)
        assert record.windows[0].hfd_arcsec == pytest.approx(3.0 * 2 * PLATE_SCALE)

    def test_frames_while_settling_are_left_out(self):
        c = connector()
        c._handle_event({"Event": "StartGuiding"})
        c.start_activity(PHD2Activities.Settling)
        c._handle_event(guide_step())
        assert c.sky_quality_while_guiding.samples == 0

    def test_guiding_already_under_way_opens_a_session(self):
        """The unit (re)connected to a PHD2 that was already guiding: no StartGuiding comes."""
        c = connector()
        c._handle_event(guide_step())
        assert c.sky_quality_while_guiding.samples == 1

    def test_guiding_stopped_ends_the_session(self):
        c = connector()
        c._handle_event({"Event": "StartGuiding"})
        c._handle_event({"Event": "GuidingStopped"})
        record = c.sky_quality_while_guiding
        assert not record.running and record.succeeded is True

    def test_star_lost_is_counted(self):
        c = connector()
        c._handle_event({"Event": "StartGuiding"})
        c._handle_event({"Event": "StarLost", "AvgDist": 1.0})
        assert c.sky_quality_while_guiding.star_lost == 1

    def test_a_guide_step_without_hfd_or_snr_is_handled(self):
        c = connector()
        c._handle_event({"Event": "StartGuiding"})
        c._handle_event({k: v for k, v in guide_step().items() if k not in ("HFD", "SNR")})
        assert c.sky_quality_while_guiding.rejected == 1


class FakeConnection:
    def __init__(self, lines):
        self.lines = list(lines)

    def read_line(self) -> str:
        return self.lines.pop(0) if self.lines else ""


def test_an_event_the_handler_fails_on_does_not_end_the_reader(caplog):
    """The reader also delivers RPC replies: ending it silenced every call to PHD2."""
    c = object.__new__(PHD2Connector)
    c.conn = FakeConnection(['{"Event": "StarLost"}', '{"jsonrpc": "2.0", "id": 7, "result": 0}'])
    c._terminate = False
    c._rpc_lock = threading.Lock()
    c._pending = {}
    delivered = []
    c._deliver = delivered.append
    c._handle_event = lambda ev: (_ for _ in ()).throw(KeyError("AvgDist"))

    with caplog.at_level("ERROR"):
        c._read_forever()

    assert delivered == [{"jsonrpc": "2.0", "id": 7, "result": 0}]
    assert "could not handle the 'StarLost' event" in caplog.text
