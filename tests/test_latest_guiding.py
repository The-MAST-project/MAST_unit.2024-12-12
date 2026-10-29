"""A guide start's record, `status.latest.guiding` (common.models.latest.LatestGuiding), and
`is_settling` saying a settle is in progress rather than that one ever started (#362)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from test_sky_quality_while_guiding import FakeConnection, connector, quiet_notifier  # noqa: F401

import guiding
from common.activities import Activities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.interfaces.guiding import GuiderTypes
from common.models.latest import GuidingSettle, LatestGuiding
from guiding import Guider
from phd2.phd2 import PHD2Connector, PHD2SettleProgress

SETTLE = SimpleNamespace(pixels=2, time=10, timeout=60)


def settle(done: bool) -> PHD2SettleProgress:
    s = PHD2SettleProgress()
    s.done = done
    return s


def phd2(**overrides) -> PHD2Connector:
    c = connector()
    c.lock = threading.Lock()
    c._connected = True
    c.guiding_record = None
    c.settle_px = 0
    c.call = lambda method, params=None, **kw: {"result": False}
    type(c).settling_settings = property(lambda self: SETTLE)  # the property reads the unit's config
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


@pytest.fixture(autouse=True)
def _restore_settling_settings():
    original = PHD2Connector.__dict__["settling_settings"]
    yield
    PHD2Connector.settling_settings = original


@pytest.mark.usefixtures("quiet_notifier")
class TestIsSettling:
    def test_a_finished_settle_is_not_one_in_progress(self):
        c = phd2()
        c._handle_event({"Event": "SettleDone", "Status": 0})
        assert c.is_settling() is False

    def test_a_settle_under_way_is(self):
        c = phd2(settle=settle(done=False))
        assert c.is_settling() is True

    def test_a_lost_connection_clears_an_unfinished_settle(self):
        """PHD2 exited mid-settle: no SettleDone will come, and `guide()` would refuse for good."""
        c = phd2(settle=settle(done=False), conn=FakeConnection([]), _terminate=False, _terminate_requested=False)
        c._rpc_lock = threading.Lock()
        c._pending = {}
        c._worker()
        assert c.settle is None

    def test_a_lost_connection_keeps_a_finished_settle(self):
        """#30's `wait_for_settle` reads the finished one to tell settled from failed."""
        finished = settle(done=True)
        c = phd2(settle=finished, conn=FakeConnection([]), _terminate=False, _terminate_requested=False)
        c._rpc_lock = threading.Lock()
        c._pending = {}
        c._worker()
        assert c.settle is finished


@pytest.mark.usefixtures("quiet_notifier")
class TestTheConnectorFeedsTheRecord:
    def test_the_settle_criterion_is_recorded(self):
        c = phd2()
        record = LatestGuiding(backend="phd2").begin()
        c.adopt_guiding_record(record)
        assert (record.settle_pixels, record.settle_time, record.settle_timeout) == (2, 10, 60)

    def test_a_settle_that_succeeded(self):
        c = phd2()
        c.adopt_guiding_record(record := LatestGuiding(backend="phd2").begin())
        c._handle_event({"Event": "SettleDone", "Status": 0})
        assert record.settle is GuidingSettle.SETTLED and record.running

    def test_a_failed_settle_carries_phd2s_error_and_guiding_goes_on(self):
        c = phd2()
        c.adopt_guiding_record(record := LatestGuiding(backend="phd2").begin())
        c._handle_event({"Event": "SettleDone", "Status": 1, "Error": "timed-out waiting for guider to settle"})
        assert record.settle is GuidingSettle.FAILED
        assert record.settle_error == "timed-out waiting for guider to settle"
        assert c.settle.error == "timed-out waiting for guider to settle"
        assert record.running

    def test_a_failure_without_an_error_text_still_fails(self):
        c = phd2()
        c.adopt_guiding_record(record := LatestGuiding(backend="phd2").begin())
        c._handle_event({"Event": "SettleDone", "Status": 1})
        assert record.settle is GuidingSettle.FAILED and record.settle_error == "status 1"

    def test_guiding_stopped_ends_it_where_the_sky_quality_session_ends(self):
        """Also a stop made in PHD2's own window, which the unit never asked for."""
        c = phd2()
        c.adopt_guiding_record(record := LatestGuiding(backend="phd2").begin())
        c._handle_event({"Event": "StartGuiding"})
        c._handle_event({"Event": "GuidingStopped"})
        assert not record.running and record.succeeded is True
        assert not c.sky_quality_while_guiding.running

    def test_a_failed_settle_then_guiding_stopped_keeps_the_settle(self):
        c = phd2()
        c.adopt_guiding_record(record := LatestGuiding(backend="phd2").begin())
        c._handle_event({"Event": "StartGuiding"})
        c._handle_event({"Event": "SettleDone", "Status": 1, "Error": "timed out"})
        assert record.running
        c._handle_event({"Event": "GuidingStopped"})
        assert record.settle is GuidingSettle.FAILED and record.succeeded is True

    def test_a_lost_connection_ends_it_failed(self):
        c = phd2(conn=FakeConnection([]), _terminate=False, _terminate_requested=False)
        c._rpc_lock = threading.Lock()
        c._pending = {}
        c.adopt_guiding_record(record := LatestGuiding(backend="phd2").begin())
        c._worker()
        assert not record.running and record.succeeded is False
        assert record.errors == ["the connection to PHD2 was lost"]


class FakeBackend:
    def __init__(self, response=CanonicalResponse_Ok, raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.release = threading.Event()
        self.started = threading.Event()
        self.is_guiding = False

    def start_guiding(self):
        self.started.set()
        self.release.wait(5)
        if self.raises is not None:
            raise self.raises
        return self.response

    def stop_guiding(self):
        pass


def guider(backend=None) -> Guider:
    g = object.__new__(Guider)
    Activities.__init__(g)
    g._backend = backend or FakeBackend()
    g.guider_type = GuiderTypes.Solving
    g.latest_guiding = None
    unit = SimpleNamespace(
        plan_owner="PLAN_G",
        acquirer=SimpleNamespace(latest_acquisition=object()),
        mount=SimpleNamespace(is_tracking=True, start_tracking=lambda: None, stop_tracking=lambda: None),
        imager=None,
        guider=g,
        was_tracking_before_guiding=True,
    )
    unit.is_active = lambda activity: False
    unit.end_activity = lambda activity: None
    g.unit = unit
    return g


class TestTheGuider:
    def test_the_record_is_fresh_before_the_endpoint_answers(self, monkeypatch):
        threads = []
        monkeypatch.setattr(
            guiding, "Thread", lambda **kw: SimpleNamespace(start=lambda: threads.append(kw))
        )  # not started: what the endpoint itself left behind
        g = guider()
        before = LatestGuiding(backend="solving").begin()
        before.finish(True)
        g.latest_guiding = before

        assert g.endpoint_start_guiding().succeeded
        record = g.latest_guiding
        assert record is not before and record.running and record.backend == "solving"
        assert record.plan_id == "PLAN_G"
        assert threads[0]["args"] == [record], "the thread starts guiding under that record"

    def test_a_refusal_ends_it_failed(self):
        g = guider(FakeBackend(response=CanonicalResponse(errors=["binning does not match"])))
        g._backend.release.set()
        response = g.start_guiding()
        assert response.failed
        assert not g.latest_guiding.running and g.latest_guiding.succeeded is False
        assert g.latest_guiding.errors == ["binning does not match"]

    def test_a_raise_ends_it_failed(self):
        g = guider(FakeBackend(raises=RuntimeError("cannot guide while settling")))
        g._backend.release.set()
        with pytest.raises(RuntimeError):
            g.start_guiding()
        assert g.latest_guiding.succeeded is False and "cannot guide while settling" in g.latest_guiding.errors[0]

    def test_an_accepted_start_runs_until_stopped(self):
        g = guider()
        g._backend.release.set()
        g.start_guiding()
        assert g.latest_guiding.running
        g.stop_guiding()
        assert not g.latest_guiding.running and g.latest_guiding.succeeded is True

    def test_stopping_acquisition_and_guiding_ends_it_even_when_not_guiding(self):
        """The solving guider's `is_guiding` is the unit's Guiding flag, cleared just before."""
        g = guider()
        g._backend.release.set()
        g.start_guiding()
        g.stop_acquisition_and_guiding()
        assert not g.latest_guiding.running and g.latest_guiding.succeeded is True

    def test_in_the_unit_status(self):
        from unit import Unit

        g = guider()
        g.latest_guiding = LatestGuiding(backend="phd2").begin()
        u = object.__new__(Unit)
        u.focus_on_star = SimpleNamespace(latest=None)
        u.acquirer = None
        u.latest_exposure = None
        u.guider = g
        assert Unit._latest_records(u).guiding is g.latest_guiding
