"""The unit waits for PHD2 to come up, rather than sleeping at it (#254).

`__init__` used to launch `phd2.exe`, sleep a flat three seconds and connect once.
Three seconds is the warm case. On mast01 on 2026-09-22 a cold start took longer,
both the imager and the guider were marked permanently failed, and twelve minutes
later PHD2 was running and listening on 4400 with the unit unable to talk to it
until the service was restarted by hand. The instrument looked broken; the cause
had already resolved itself.

The timeout message carries elapsed time and attempt count on purpose: "PHD2 is
broken" and "PHD2 was slower than we waited" want different responses, and the
old failure distinguished them not at all.
"""

from __future__ import annotations

import pytest

try:
    from phd2.phd2 import PHD2_STARTUP_POLL, PHD2_STARTUP_TIMEOUT, PHD2Connector, PHD2ConnectorError
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


class Phd2ThatStartsSlowly:
    """Refuses `refusals` times, then connects -- a PHD2 still opening its port.

    `refusals=None` never answers at all, which is the broken-PHD2 case.
    """

    def __init__(self, refusals: int | None):
        self.remaining = refusals
        self.attempts = 0

    def __call__(self):
        self.attempts += 1
        if self.remaining is None:
            raise ConnectionRefusedError("No connection could be made")
        if self.remaining > 0:
            self.remaining -= 1
            raise ConnectionRefusedError("No connection could be made")


def connector(connect):
    c = object.__new__(PHD2Connector)
    c.connect = connect
    return c


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch):
    """Advance time only when the code sleeps, so a 30 s deadline costs no seconds.

    Stubbing `sleep` alone is not enough and is worse than nothing: the loop then
    spins with the clock frozen, the deadline never arrives, and a test meant to
    prove the timeout instead proves the retry.
    """
    now = [0.0]
    monkeypatch.setattr("phd2.phd2.time.monotonic", lambda: now[0])
    monkeypatch.setattr("phd2.phd2.time.sleep", lambda s: now.__setitem__(0, now[0] + s))


class TestItWaits:
    def test_a_phd2_that_answers_at_once_costs_one_attempt(self):
        phd2 = Phd2ThatStartsSlowly(refusals=0)
        connector(phd2)._await_phd2()
        assert phd2.attempts == 1

    def test_it_keeps_trying_while_phd2_is_still_starting(self):
        """The case the flat sleep lost: slow, then fine."""
        phd2 = Phd2ThatStartsSlowly(refusals=6)
        connector(phd2)._await_phd2()
        assert phd2.attempts == 7

    def test_it_tries_many_times_within_the_deadline(self):
        """A 0.5 s poll over 30 s is dozens of chances, not one."""
        phd2 = Phd2ThatStartsSlowly(refusals=40)
        connector(phd2)._await_phd2()
        assert phd2.attempts == 41
        assert PHD2_STARTUP_TIMEOUT / PHD2_STARTUP_POLL >= 40


class TestItGivesUp:
    def test_a_phd2_that_never_answers_raises(self):
        phd2 = Phd2ThatStartsSlowly(refusals=None)
        with pytest.raises(PHD2ConnectorError):
            connector(phd2)._await_phd2(timeout=2.0)

    def test_the_message_says_how_long_it_waited_and_how_often_it_tried(self):
        """So a slow PHD2 and a broken one stop looking identical."""
        phd2 = Phd2ThatStartsSlowly(refusals=None)
        with pytest.raises(PHD2ConnectorError) as caught:
            connector(phd2)._await_phd2(timeout=2.0)
        message = str(caught.value)
        assert "attempts" in message
        assert "did not answer within" in message

    def test_it_carries_the_underlying_error(self):
        """The last refusal is the diagnosis; losing it leaves only 'timed out'."""
        phd2 = Phd2ThatStartsSlowly(refusals=None)
        with pytest.raises(PHD2ConnectorError) as caught:
            connector(phd2)._await_phd2(timeout=2.0)
        assert isinstance(caught.value.__cause__, ConnectionRefusedError)


def test_no_flat_sleep_survives_in_init():
    """The regression this replaces: a fixed wait before a single attempt."""
    import inspect

    source = inspect.getsource(PHD2Connector.__init__)
    assert "sleeping" not in source, "the flat startup sleep is back"
