"""The cooler verdict, logged once per cool-down for whatever scrapes the logs (#374).

On mast04, night of 2026-10-19, the guide camera sat at 47.7 C against a 5.0 C set point at
100 % cooler power, and the only trace in the unit log was an untagged warning nothing
collected. The verdict line is a contract with the scraper, so its shape is pinned here.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import imagers as imagers_module
from imagers import Imager
from imagers.cooler import CoolerState, log_cooler_verdict

#: What a scraper keys on. Changing the line means changing this, which is the point.
VERDICT = re.compile(
    r"^(?:DEGRADED )?cooler=(regulating|unreached) temp=(-?\d+\.\d) set-point=(-?\d+\.\d) "
    r"power=(\d+|unknown) after=(\d+)s"
)


def _message(caplog) -> tuple[str, str]:
    (record,) = caplog.records
    return record.levelname, record.getMessage()


def test_a_set_point_not_reached_is_logged_degraded(caplog):
    with caplog.at_level("INFO"):
        log_cooler_verdict(CoolerState.Unreached, 47.7, 5.0, 100.0, 900.4)

    level, message = _message(caplog)
    assert level == "WARNING"
    assert message.startswith("DEGRADED cooler=unreached temp=47.7 set-point=5.0 power=100 after=900s ")
    assert VERDICT.match(message)


def test_a_set_point_reached_is_logged_healthy(caplog):
    with caplog.at_level("INFO"):
        log_cooler_verdict(CoolerState.Regulating, 5.4, 5.0, 31.0, 412.0)

    level, message = _message(caplog)
    assert level == "INFO"
    assert message == "cooler=regulating temp=5.4 set-point=5.0 power=31 after=412s"
    assert VERDICT.match(message)


def test_an_unread_power_is_logged_unknown(caplog):
    with caplog.at_level("INFO"):
        log_cooler_verdict(CoolerState.Regulating, -10.0, -10.0, None, 60.0)

    assert VERDICT.match(_message(caplog)[1]).group(4) == "unknown"


# --- from the cool-down watcher ---------------------------------------------------------


class _Backend:
    connected = True
    set_point = 5.0

    def __init__(self, temperature: float, power_raises: bool = False):
        self.temperature = temperature
        self._power_raises = power_raises

    @property
    def cooler_power(self):
        if self._power_raises:
            raise RuntimeError("rpc failed")
        return 100.0


def _imager(backend) -> Imager:
    imager = object.__new__(Imager)
    imager._backend = backend
    imager.unit = SimpleNamespace(unit_conf=SimpleNamespace(imager=SimpleNamespace(temp_check_interval=10)))
    active = {True}
    imager.is_active = lambda _activity: bool(active)
    imager.end_activity = lambda _activity, **_: active.clear()
    return imager


@pytest.fixture
def one_poll(monkeypatch):
    """Past the slow-cooling deadline from the first poll; disconnect after it."""
    monkeypatch.setattr(imagers_module, "COOLING_SLOW_WARNING_SECONDS", -1.0)

    def factory(backend):
        monkeypatch.setattr(imagers_module.time, "sleep", lambda _s: setattr(backend, "connected", False))

    return factory


def test_the_watcher_logs_unreached_with_the_cooler_power(caplog, one_poll):
    backend = _Backend(temperature=47.7)
    one_poll(backend)

    with caplog.at_level("WARNING"):
        _imager(backend).do_cool_down()

    assert "DEGRADED cooler=unreached temp=47.7 set-point=5.0 power=100 " in caplog.text


def test_a_failed_power_read_still_logs_the_verdict(caplog, one_poll):
    backend = _Backend(temperature=47.7, power_raises=True)
    one_poll(backend)

    with caplog.at_level("WARNING"):
        _imager(backend).do_cool_down()

    assert "DEGRADED cooler=unreached temp=47.7 set-point=5.0 power=unknown " in caplog.text


def test_the_watcher_logs_regulating_when_it_reaches_the_set_point(caplog, one_poll):
    backend = _Backend(temperature=5.4)
    one_poll(backend)

    with caplog.at_level("INFO"):
        _imager(backend).do_cool_down()

    assert "cooler=regulating temp=5.4 set-point=5.0 power=100 " in caplog.text
    assert "DEGRADED" not in caplog.text
