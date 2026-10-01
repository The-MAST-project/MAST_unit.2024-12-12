"""A dead imager is logged once per outage, not once per status poll (#260).

`Imager.status()` read the backend's hardware properties unconditionally, and every PHD2
getter logged its failure with a full traceback. Nothing in the unit polls status on its own;
the GUI does, on a timer, through the controller. So on mast01 on 2026-09-22, with the PHD2
connector down after losing a port race (#254), each poll logged four tracebacks and
buried the acquisition and guiding lines the run depended on.

Two situations produce the flood, and each has its own guard:

- **The backend is not connected.** `Imager.status()` then answers from local state and
  touches nothing on the wire.
- **The backend is connected but its reads fail**, for example PHD2 still running with the
  camera's outlet off. Each getter then logs the first failure and the recovery, not the
  failures in between.
"""

from __future__ import annotations

import logging

import pytest

from common.activities import ImagerActivities
from common.models.statuses import PHD2ImagerStatus
from failure_streaks import FailureStreaks
from imagers import Imager
from phd2.phd2 import PHD2Connector, PHD2ConnectorError

POLLS = 5
HARDWARE_READS = ("temperature", "set_point", "cooler_on", "cooler_power")


class Backend(PHD2Connector):
    """A backend whose hardware reads must not happen while it is disconnected."""

    def __init__(self):
        self.is_connected = False

    @property
    def connected(self) -> bool:
        return self.is_connected

    @property
    def name(self) -> str:
        return "fake"

    def status(self) -> PHD2ImagerStatus:
        return PHD2ImagerStatus(connected=self.is_connected)

    def _hardware(self, what: str) -> None:
        if not self.is_connected:
            raise AssertionError(f"read {what} from a disconnected backend")

    @property
    def temperature(self) -> float | None:
        return self._hardware("temperature")

    @property
    def set_point(self) -> float | None:
        return self._hardware("set_point")

    @property
    def cooler_on(self) -> bool | None:
        return self._hardware("cooler_on")

    @property
    def cooler_power(self) -> float | None:
        return self._hardware("cooler_power")


class PoweredImager(Imager):
    """An `Imager` built without its constructor, whose outlet reads as on."""

    def is_on(self) -> bool:
        return True


def disconnected_imager(backend: Backend) -> Imager:
    imager = object.__new__(PoweredImager)
    imager._backend = backend
    imager._streaks = FailureStreaks()
    imager.latest_settings = None
    imager.activities = ImagerActivities.Idle
    return imager


class TestADisconnectedImagerIsNotProbed:
    def test_status_reads_no_hardware_value(self):
        status = disconnected_imager(Backend()).status()

        assert status.connected is False
        assert (status.temperature, status.set_point, status.cooler_on, status.cooler_power) == (None,) * 4

    def test_the_outage_is_logged_once_across_polls(self, caplog):
        imager = disconnected_imager(Backend())

        with caplog.at_level(logging.ERROR):
            for _ in range(POLLS):
                imager.status()

        assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
        assert not [r for r in caplog.records if r.exc_info]

    def test_a_reconnect_is_logged_and_rearms_the_next_outage(self, caplog):
        backend = Backend()
        imager = disconnected_imager(backend)
        imager.status()

        backend.is_connected = True
        with caplog.at_level(logging.INFO):
            imager.status()
        assert [r for r in caplog.records if r.levelno == logging.INFO]
        backend.is_connected = False

        with caplog.at_level(logging.ERROR):
            caplog.clear()
            imager.status()

        assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


class Connector(PHD2Connector):
    """A PHD2 connector whose every RPC either fails or answers, as `failing` says."""

    def __init__(self):
        self.failing = True
        self.errors = []
        self._read_failures = FailureStreaks()

    def call(self, method, params=None, timeout: float = 0):
        if self.failing:
            raise PHD2ConnectorError(f"error from RPC: {method=}, message=Camera not connected")
        return {"result": {"coolerOn": True, "temperature": 5.1, "setpoint": 5.0, "power": 42.0}}


@pytest.mark.parametrize("read", HARDWARE_READS)
class TestAFailingReadIsLoggedOncePerOutage:
    def test_repeated_failures_log_one_line_and_no_traceback(self, read, caplog):
        p = Connector()

        with caplog.at_level(logging.ERROR):
            values = [getattr(p, read) for _ in range(POLLS)]

        assert values == [None] * POLLS
        assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
        assert not [r for r in caplog.records if r.exc_info]
        assert len(p.errors) == 1

    def test_a_recovery_is_logged_and_the_next_outage_is_logged_again(self, read, caplog):
        p = Connector()
        getattr(p, read)

        p.failing = False
        with caplog.at_level(logging.INFO):
            caplog.clear()
            assert getattr(p, read) is not None
        assert [r for r in caplog.records if r.levelno == logging.INFO]

        p.failing = True
        with caplog.at_level(logging.ERROR):
            caplog.clear()
            getattr(p, read)
        assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


class TestFailureStreaks:
    def test_only_the_first_failure_begins_a_streak(self):
        streaks = FailureStreaks()

        assert [streaks.begins("x") for _ in range(3)] == [True, False, False]

    def test_only_the_first_success_after_a_streak_ends_it(self):
        streaks = FailureStreaks()
        streaks.begins("x")

        assert [streaks.ends("x") for _ in range(3)] == [True, False, False]

    def test_a_success_with_no_streak_ends_nothing(self):
        assert FailureStreaks().ends("x") is False

    def test_keys_are_independent(self):
        streaks = FailureStreaks()
        streaks.begins("x")

        assert streaks.begins("y") is True
