"""A flag raised before an RPC is ended however that RPC turns out (MAST_unit#236).

`tests/contract/test_activity_flag_balance.py` cannot see these: it is an AST scan at
enum granularity, and both members appear in a `start_activity` and an `end_activity`
somewhere under `src/`, so it passes. Balance *within a call* is a behavioural question.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from common.activities import ImagerActivities
from common.notifications import Notifier
from phd2.phd2 import PHD2Activities, PHD2Connector, PHD2ConnectorError


def make_connector(activity_spy) -> PHD2Connector:
    p = object.__new__(PHD2Connector)
    p._connected = True
    p.lock = threading.Lock()
    p.errors = []
    p.parent = activity_spy
    p.activities = PHD2Activities(0)
    p.timings = {}
    p.details = {}
    p.data = {}
    p.image_saved_event = threading.Event()
    p.image_was_saved = False
    return p


class TestStopExposure:
    """`stop_capture` goes over the RPC that may be the reason the stop was needed."""

    def test_it_ends_both_flags_when_the_stop_fails(self, activity_spy, monkeypatch):
        p = make_connector(activity_spy)
        activity_spy.start_activity(ImagerActivities.Exposing)
        activity_spy.start_activity(ImagerActivities.Saving)
        monkeypatch.setattr(p, "call", lambda *a, **k: (_ for _ in ()).throw(PHD2ConnectorError("no answer")))

        with pytest.raises(PHD2ConnectorError):
            p.stop_exposure()

        assert activity_spy.activities == set(), "a failed stop must still unwind"

    def test_it_ends_saving_on_the_happy_path_too(self, activity_spy, monkeypatch):
        """`Saving` was never ended here at all, even when the stop succeeded."""
        p = make_connector(activity_spy)
        activity_spy.start_activity(ImagerActivities.Exposing)
        activity_spy.start_activity(ImagerActivities.Saving)
        monkeypatch.setattr(p, "call", lambda *a, **k: None)

        p.stop_exposure()

        assert activity_spy.activities == set()

    def test_it_releases_a_waiter(self, activity_spy, monkeypatch):
        """Nothing else sets the event once the capture has been stopped."""
        p = make_connector(activity_spy)
        monkeypatch.setattr(p, "call", lambda *a, **k: None)

        p.stop_exposure()

        assert p.image_saved_event.is_set()


class TestStartGuidingHandover:
    """`EquipmentHandover` was raised once and ended on no path at all."""

    @pytest.fixture(autouse=True)
    def _quiet_conf(self, monkeypatch):
        """Raising a flag emits a UI notification, which reads the connector's `conf`.

        That reaches a Mongo-backed `Config` -- so asserting on a flag would otherwise
        need a configuration database. Stubbed rather than provided: nothing here is
        about configuration.
        """
        monkeypatch.setattr(PHD2Connector, "conf", property(lambda self: MagicMock()))
        # `Notifier()` is a singleton whose __init__ imports the notification API and
        # loads the local configuration. Pre-seed the instance so construction is a
        # no-op rather than a file read.
        monkeypatch.setattr(Notifier, "_instance", object.__new__(Notifier))
        monkeypatch.setattr(Notifier, "_initialized", True)
        monkeypatch.setattr(Notifier, "ui_notification", lambda *a, **k: None)

    def _connector(self, activity_spy):
        p = make_connector(activity_spy)
        p._connected = False
        return p

    def test_it_ends_the_handover_when_connecting_fails(self, activity_spy, monkeypatch):
        p = self._connector(activity_spy)
        monkeypatch.setattr(p, "connect", lambda: (_ for _ in ()).throw(PHD2ConnectorError("refused")))

        response = p.start_guiding()

        assert response.failed
        assert not p.is_active(PHD2Activities.EquipmentHandover)

    def test_it_ends_the_handover_when_connecting_succeeds(self, activity_spy, monkeypatch):
        """The success path fell straight through without ending it."""
        p = self._connector(activity_spy)
        monkeypatch.setattr(p, "connect", lambda: None)
        monkeypatch.setattr(p, "connect_equipment", lambda: None)

        # Everything past the handover needs a real unit, and the next line asserts on
        # one. The flag is settled before that, which is the whole claim here.
        with pytest.raises(AssertionError):
            p.start_guiding()

        assert not p.is_active(PHD2Activities.EquipmentHandover)


class TestExposeActsOnTheResponse:
    """`_expose_repeatedly` discarded the envelope while every other caller checked it."""

    def _imager(self, response):
        imager = MagicMock()
        imager.start_exposure = MagicMock(return_value=response)
        return imager

    def test_it_raises_when_the_imager_refuses(self):
        from common.canonical import CanonicalResponse
        from unit import _start_exposure_or_raise

        imager = self._imager(CanonicalResponse(errors=["no camera"]))

        with pytest.raises(RuntimeError, match="refused the exposure"):
            _start_exposure_or_raise(imager, MagicMock())

    def test_it_returns_quietly_when_the_exposure_starts(self):
        from common.canonical import CanonicalResponse
        from unit import _start_exposure_or_raise

        imager = self._imager(CanonicalResponse(value=None))

        _start_exposure_or_raise(imager, MagicMock())
