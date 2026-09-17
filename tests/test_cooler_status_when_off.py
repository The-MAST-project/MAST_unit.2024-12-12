"""Reading the cooler state must not require the cooler to be on (#109).

PHD2's `get_cooler_status` sends `setpoint` and `power` **only when the cooler is on** --
`event_server.cpp`:

    rslt << NV("coolerOn", on) << NV("temperature", temperature, 1);
    if (on)
        rslt << NV("setpoint", setpoint, 1) << NV("power", power, 1);

while `cooler_on` guarded on `coolerOn` and then read `setpoint`, so the guard passed on
exactly the reply that lacks the key. The `KeyError` was swallowed by the property's own
`except`, which returned `None` -- indistinguishable from "could not reach PHD2".

Measured on mast01 over 2026-09-14/15: **75 tracebacks**, in the two windows the cooler was
off (53 between 21:20:49Z and 21:42:07Z, 22 more between 00:33 and 00:34Z after a service
restart), each ending the moment the cooler came on. Through both, `/imager/status` recorded
`cooler_on` and `cooler_power` as null while the sensor climbed 16.5 -> 19.0 degC -- and the
off-setpoint alarm reported correctly only because `self._setpoint` still held 5.0 from an
earlier successful read. On a unit whose cooler had never been on it would have been `None`,
and the alarm would have been silent through the episode it exists for.

The sibling `cooler_power` two properties below already guards on its own key, so this was an
inconsistency inside one class rather than a considered choice.
"""

from __future__ import annotations

import logging

import pytest

from phd2.phd2 import PHD2Connector

SET_POINT = 5.0


def make_connector(reply: dict | None) -> PHD2Connector:
    """A PHD2Connector with nothing built but the one call the property makes."""
    p = object.__new__(PHD2Connector)
    p._setpoint = None
    p.call = lambda method, *a, **k: reply  # type: ignore[method-assign]
    return p


def cooler_off_reply() -> dict:
    """What PHD2 actually sends with the cooler off: no setpoint, no power."""
    return {"result": {"coolerOn": False, "temperature": 19.0}}


def cooler_on_reply() -> dict:
    return {"result": {"coolerOn": True, "temperature": 5.1, "setpoint": SET_POINT, "power": 42.0}}


class TestTheCoolerCanBeReadWhileOff:
    def test_a_cooler_that_is_off_reports_false_rather_than_none(self):
        connector = make_connector(cooler_off_reply())

        assert connector.cooler_on is False

    def test_reading_an_off_cooler_logs_no_exception(self, caplog):
        """`None` and a traceback per poll is how a routine state looked like a fault."""
        connector = make_connector(cooler_off_reply())

        with caplog.at_level(logging.ERROR):
            _ = connector.cooler_on

        assert not [r for r in caplog.records if r.exc_info]

    def test_a_cooler_that_is_on_still_reports_true(self):
        connector = make_connector(cooler_on_reply())

        assert connector.cooler_on is True


class TestTheSetPointIsRead:
    """There is no set-point cache any more (#245).

    The previous version of this class pinned the cache as *current* behaviour
    rather than wanted, so that removing it would be a deliberate act instead of a
    side effect. This is that act: `set_point` now asks PHD2 every time, and a
    cooler that is off has no set point to report.
    """

    def test_it_reads_the_set_point_while_the_cooler_is_on(self):
        assert make_connector(cooler_on_reply()).set_point == SET_POINT

    def test_a_cooler_that_is_off_reports_none(self):
        """PHD2 sends `setpoint` only inside `if (on)`, so None is the truthful answer."""
        assert make_connector(cooler_off_reply()).set_point is None

    def test_a_set_point_does_not_survive_the_cooler_going_off(self):
        """The 2026-09-14/15 episode: 80 minutes at 16.5 degC still reporting 5.0.

        The alarm built for that episode fired on a value held over from an earlier
        read. It happened to be right; on a unit whose cooler had never been on it
        would have been None and nothing would have fired at all.
        """
        connector = make_connector(cooler_on_reply())
        assert connector.set_point == SET_POINT

        connector.call = lambda method, *a, **k: cooler_off_reply()  # type: ignore[method-assign]

        assert connector.set_point is None

    def test_every_read_asks_phd2_again(self):
        calls: list[str] = []

        def counting(method, *a, **k):
            calls.append(method)
            return cooler_on_reply()

        connector = make_connector(cooler_on_reply())
        connector.call = counting  # type: ignore[method-assign]
        _ = connector.set_point
        _ = connector.set_point

        assert calls.count("get_cooler_status") == 2


class TestAnUnreadableCoolerIsStillNone:
    @pytest.mark.parametrize("reply", [None, {}, {"result": {}}, {"result": {"temperature": 19.0}}])
    def test_a_reply_that_does_not_say_reports_none(self, reply):
        """The one case `None` is the honest answer: PHD2 did not say whether it is on."""
        connector = make_connector(reply)

        assert connector.cooler_on is None
