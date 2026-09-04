"""``wait_for_settle`` reports which ending it saw, not a bare bool (issue #86).

A settle PHD2 rejected, a settle still running when the wait expired, and a settle
that never started are three faults with three different fixes, and the fold-mirror
SPEC handover is the caller that has to tell them apart. These tests pin that the
four outcomes are distinguishable and that the elapsed time is the one actually
waited -- an outcome misreported as a timeout hides the fault behind it.

Runs in the unit venv (Windows): the import chain is Windows-only today
(``stage.py`` uses pyximc names at module level). Skips cleanly elsewhere.
"""

from __future__ import annotations

import threading

import pytest

try:
    from phd2.phd2 import PHD2Connector
except (ImportError, NameError) as ex:  # NameError: stage.py off-Windows
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from phd2.phd2 import PHD2SettleProgress, SettleOutcome

# wait_for_settle polls on a 1 s sleep, so a wait that must expire costs real seconds.
# Keep it short: the endings that return early do so on the first pass, before any sleep.
EXPIRING_TIMEOUT = 2.0


def make_connector(settle: PHD2SettleProgress | None) -> PHD2Connector:
    """A real PHD2Connector minus the heavy __init__. wait_for_settle reads exactly
    two attributes, and `lock` must be a real lock -- `object.__new__` provides neither."""
    p = object.__new__(PHD2Connector)
    p.lock = threading.Lock()
    p.settle = settle
    return p


def settle_progress(*, done: bool, status: int = 0, error=None) -> PHD2SettleProgress:
    s = PHD2SettleProgress()
    s.done = done
    s.status = status
    s.error = error
    return s


class TestTheEndingsAreDistinguishable:
    def test_a_clean_settle_is_settled(self):
        result = make_connector(settle_progress(done=True)).wait_for_settle(timeout=EXPIRING_TIMEOUT)
        assert result.outcome is SettleOutcome.SETTLED
        assert result.settled

    def test_a_settle_phd2_rejected_is_failed_not_timed_out(self):
        result = make_connector(settle_progress(done=True, status=1)).wait_for_settle(timeout=EXPIRING_TIMEOUT)
        assert result.outcome is SettleOutcome.FAILED
        assert not result.settled

    def test_an_error_string_is_a_failure_even_with_status_zero(self):
        settle = settle_progress(done=True, status=0, error="star lost")
        result = make_connector(settle).wait_for_settle(timeout=EXPIRING_TIMEOUT)
        assert result.outcome is SettleOutcome.FAILED

    def test_a_settle_still_running_when_the_wait_expires_is_timed_out(self):
        result = make_connector(settle_progress(done=False)).wait_for_settle(timeout=EXPIRING_TIMEOUT)
        assert result.outcome is SettleOutcome.TIMED_OUT

    def test_nothing_settling_at_all_is_never_reported(self):
        result = make_connector(None).wait_for_settle(timeout=EXPIRING_TIMEOUT)
        assert result.outcome is SettleOutcome.NEVER_REPORTED


class TestTheElapsedTimeIsMeasured:
    def test_a_rejected_settle_returns_immediately_and_says_so(self):
        """A rejected settle must not borrow the timeout it never reached."""
        result = make_connector(settle_progress(done=True, status=1)).wait_for_settle(timeout=600.0)
        assert result.elapsed < 1.0
        assert "600" not in str(result)

    def test_an_expiring_wait_reports_roughly_its_timeout(self):
        result = make_connector(None).wait_for_settle(timeout=EXPIRING_TIMEOUT)
        assert EXPIRING_TIMEOUT <= result.elapsed < EXPIRING_TIMEOUT + 2.0


class TestWhatTheCallerLogs:
    def test_a_failure_carries_phd2s_own_status_and_error(self):
        settle = settle_progress(done=True, status=1, error="timed-out")
        result = make_connector(settle).wait_for_settle(timeout=EXPIRING_TIMEOUT)
        assert "status=1" in result.detail
        assert "timed-out" in result.detail

    def test_each_ending_renders_distinctly(self):
        rendered = {
            str(make_connector(settle_progress(done=True)).wait_for_settle(timeout=EXPIRING_TIMEOUT)),
            str(make_connector(settle_progress(done=True, status=1)).wait_for_settle(timeout=EXPIRING_TIMEOUT)),
            str(make_connector(settle_progress(done=False)).wait_for_settle(timeout=EXPIRING_TIMEOUT)),
            str(make_connector(None).wait_for_settle(timeout=EXPIRING_TIMEOUT)),
        }
        assert len(rendered) == 4, rendered
