"""The ASCOM backend's set point is read from the driver, not echoed (#109).

`set_point` returned `operational_set_point`, the value last *requested*, so a camera that
clamped or refused the request still reported -25. It now reads ASCOM's `SetCCDTemperature`.

The backend is never constructed (that opens a COM driver); the property runs on a stand-in
with `ascom_run` replaced.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import imagers.ascom as ascom
from common.canonical import CanonicalResponse


def _read_set_point(monkeypatch, response, connected=True):
    sent = []

    def ascom_run(_obj, sentence, *a, **k):
        sent.append(sentence)
        return response

    monkeypatch.setattr(ascom, "ascom_run", ascom_run)
    backend = SimpleNamespace(connected=connected, operational_set_point=-25)
    return ascom.ASCOMImager.set_point.fget(backend), sent  # type: ignore[attr-defined]


def test_it_reports_what_the_driver_holds_not_what_was_requested(monkeypatch):
    value, sent = _read_set_point(monkeypatch, CanonicalResponse(value=-20.0))

    assert value == -20.0, "the camera clamped -25 to -20; the old echo would say -25"
    assert sent == ["SetCCDTemperature"]


@pytest.mark.parametrize(
    "response",
    [CanonicalResponse(errors=["PropertyNotImplemented"]), CanonicalResponse(value=None)],
    ids=["driver-refuses", "no-value"],
)
def test_a_driver_that_will_not_say_reads_as_none(monkeypatch, response):
    value, _ = _read_set_point(monkeypatch, response)

    assert value is None


def test_disconnected_it_is_none_without_asking(monkeypatch):
    value, sent = _read_set_point(monkeypatch, CanonicalResponse(value=-20.0), connected=False)

    assert value is None
    assert sent == []
