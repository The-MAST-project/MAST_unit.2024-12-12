"""The mirror-first handover: fold mirror in before guiding, ready once guiding has settled (#30).

Ready-for-exposure is `Guiding` without `PreGuiding`. What is pinned here:

- `PreGuiding` goes up with `Guiding`, not when PHD2 later reports `StartGuiding`.
  PHD2 sends that only after it has looped, picked a star and calibrated; raising
  it there leaves a window in which the unit reads ready with no star at all, and
  a session that never gets that far reads ready for good.
- a settle that fails or times out stops guiding and ends both flags, rather than
  reading ready on an unsettled lock.
- the nudge runs only on the acquirer's own handover; a manually started session
  keeps the operator's pointing.
- a stop during the handover is final: the thread finds `PreGuiding` gone and does
  nothing more.
- the handover starts once `guide` is sent, not on `StartGuiding`, which PHD2 never
  sends when it fails before guiding (no star, a calibration abort); a guide request
  that fails outright lowers both flags.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from common.activities import UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config.phd2 import HandoverConfig, HandoverStyle, LockNudgeConfig, PHD2SettleConfig
from common.config.rois import FcuVersion

try:
    from phd2.phd2 import PHD2Connector, SettleOutcome, SettleResult
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


class FakeUnit:
    """The activity bookkeeping the handover reads and writes, in the order it happened."""

    def __init__(self, *, style=HandoverStyle.MIRROR_FIRST, auto_insert=True, fcu=FcuVersion.v2, nudge=True):
        self.fcu_version = fcu
        self.unit_conf = SimpleNamespace(
            phd2=SimpleNamespace(
                handover=HandoverConfig(style=style, auto_insert=auto_insert),
                lock_nudge=LockNudgeConfig(enabled=nudge),
                settle=PHD2SettleConfig(pixels=1, time=0, timeout=1),
            )
        )
        self.active: set = set()
        self.events: list[tuple[str, UnitActivities]] = []
        self.acquirer = SimpleNamespace(
            latest_acquisition=SimpleNamespace(target_ra=1.0, target_dec=2.0, outcome=None),
        )

    def start_activity(self, activity):
        self.active.add(activity)
        self.events.append(("start", activity))

    def end_activity(self, activity):
        self.active.discard(activity)
        self.events.append(("end", activity))

    def is_active(self, activity):
        return activity in self.active


def connector(unit: FakeUnit, settle: SettleResult | None = None) -> tuple[PHD2Connector, list]:
    """A connector carrying only what the handover reads; returns it and a call log."""
    calls: list = []
    c = object.__new__(PHD2Connector)
    c.parent = SimpleNamespace(unit=unit)
    c._guide_session = 0
    c._nudge_onto_target = False
    result = settle or SettleResult(SettleOutcome.SETTLED, 1.0)
    c.wait_for_settle = lambda timeout: result
    c.stop_guiding = lambda: calls.append("stop_guiding")
    return c, calls


def nudges(calls: list):
    """Patch the nudge itself, recording that it ran."""
    return patch("phd2.phd2.nudge_lock_to_target", lambda *a, **kw: calls.append("nudge"))


def begin(c: PHD2Connector, unit: FakeUnit, *, acquiring: bool) -> None:
    if acquiring:
        unit.active.add(UnitActivities.Acquiring)
    c._begin_guide_session()


class TestWhichHandover:
    def test_mirror_first_is_the_default(self):
        c, _ = connector(FakeUnit())
        assert c._handover_for_session() == c.do_mirror_first_handover

    def test_bracketed_when_selected(self):
        c, _ = connector(FakeUnit(style=HandoverStyle.BRACKETED))
        assert c._handover_for_session() == c.do_fcu_v2_spec_handover

    def test_none_when_the_caller_owns_the_insertion(self):
        c, _ = connector(FakeUnit(auto_insert=False))
        assert c._handover_for_session() is None

    def test_none_on_fcu_v1(self):
        c, _ = connector(FakeUnit(fcu=FcuVersion.v1))
        assert c._handover_for_session() is None


class TestReadiness:
    def test_preguiding_goes_up_before_guiding(self):
        """No reader may see Guiding without PreGuiding while PHD2 is still picking a star."""
        unit = FakeUnit()
        c, _ = connector(unit)
        begin(c, unit, acquiring=True)
        starts = [a for kind, a in unit.events if kind == "start"]
        assert starts.index(UnitActivities.PreGuiding) < starts.index(UnitActivities.Guiding)

    def test_no_preguiding_without_a_handover(self):
        """Nothing would end it: the unit would report work that is not happening."""
        unit = FakeUnit(auto_insert=False)
        c, _ = connector(unit)
        begin(c, unit, acquiring=True)
        assert UnitActivities.PreGuiding not in unit.active
        assert UnitActivities.Guiding in unit.active

    def test_settled_is_ready(self):
        unit = FakeUnit()
        c, calls = connector(unit)
        begin(c, unit, acquiring=True)
        with nudges(calls):
            c.do_mirror_first_handover(c._guide_session)
        assert UnitActivities.Guiding in unit.active
        assert UnitActivities.PreGuiding not in unit.active
        assert "stop_guiding" not in calls

    @pytest.mark.parametrize("outcome", [SettleOutcome.FAILED, SettleOutcome.TIMED_OUT, SettleOutcome.NEVER_REPORTED])
    def test_an_unsettled_session_is_stopped_not_ready(self, outcome):
        unit = FakeUnit()
        c, calls = connector(unit, SettleResult(outcome, 1.0))
        begin(c, unit, acquiring=True)
        with nudges(calls):
            c.do_mirror_first_handover(c._guide_session)
        assert calls == ["stop_guiding"]
        assert UnitActivities.Guiding not in unit.active
        assert UnitActivities.PreGuiding not in unit.active

    def test_a_failing_nudge_does_not_block_ready(self):
        unit = FakeUnit()
        c, _ = connector(unit)
        begin(c, unit, acquiring=True)

        def explode(*a, **kw):
            raise RuntimeError("solver down")

        with patch("phd2.phd2.nudge_lock_to_target", explode):
            c.do_mirror_first_handover(c._guide_session)
        assert UnitActivities.PreGuiding not in unit.active
        assert UnitActivities.Guiding in unit.active


class TestTheNudge:
    def test_the_acquisition_handover_nudges(self):
        unit = FakeUnit()
        c, calls = connector(unit)
        begin(c, unit, acquiring=True)
        with nudges(calls):
            c.do_mirror_first_handover(c._guide_session)
        assert calls == ["nudge"]

    def test_a_manual_session_is_not_nudged(self):
        """The operator tuned the pointing; pulling the lock back to the solve undoes it."""
        unit = FakeUnit()
        c, calls = connector(unit)
        begin(c, unit, acquiring=False)
        with nudges(calls):
            c.do_mirror_first_handover(c._guide_session)
        assert calls == []
        assert UnitActivities.PreGuiding not in unit.active

    def test_a_manual_session_still_waits_for_settle(self):
        unit = FakeUnit()
        c, _ = connector(unit)
        begin(c, unit, acquiring=False)
        assert UnitActivities.PreGuiding in unit.active


class TestStopped:
    def test_a_stop_during_the_bracketed_nudge_leaves_the_mirror_and_flags_alone(self):
        """The nudge can take up to `frames_timeout` + `confirm_timeout`; a session stopped
        meanwhile must not get the stage moved or `Guiding` raised again."""
        unit = FakeUnit(style=HandoverStyle.BRACKETED)
        c, calls = connector(unit)
        c.pause = lambda full=False: calls.append("pause")
        unit.required_stage = SimpleNamespace(move_to_preset=lambda preset: calls.append("stage"))
        begin(c, unit, acquiring=True)

        def stopped_meanwhile(*a, **kw):
            calls.append("nudge")
            unit.end_activity(UnitActivities.PreGuiding)  # what stop_acquisition_and_guiding does
            unit.end_activity(UnitActivities.Guiding)

        with patch("phd2.phd2.nudge_lock_to_target", stopped_meanwhile):
            c.do_fcu_v2_spec_handover(c._guide_session)
        assert calls == ["nudge"]
        assert UnitActivities.Guiding not in unit.active

    def test_a_stop_during_the_settle_is_final(self):
        unit = FakeUnit()
        c, calls = connector(unit, SettleResult(SettleOutcome.FAILED, 1.0))
        begin(c, unit, acquiring=True)
        session = c._guide_session
        unit.end_activity(UnitActivities.PreGuiding)  # what stop_acquisition_and_guiding does
        unit.end_activity(UnitActivities.Guiding)
        with nudges(calls):
            c.do_mirror_first_handover(session)
        assert calls == []

    def test_a_later_session_is_not_touched_by_an_earlier_handover(self):
        unit = FakeUnit()
        c, calls = connector(unit, SettleResult(SettleOutcome.TIMED_OUT, 1.0))
        begin(c, unit, acquiring=True)
        stale = c._guide_session
        begin(c, unit, acquiring=True)
        with nudges(calls):
            c.do_mirror_first_handover(stale)
        assert calls == []
        assert UnitActivities.PreGuiding in unit.active


class TestTheSession:
    def run(self, c: PHD2Connector, guide) -> tuple[object, list]:
        started: list = []

        class Recorder(threading.Thread):
            def __init__(self, *a, **kw):
                started.append(kw.get("target"))
                super().__init__(*a, **kw)

            def start(self):
                pass

        c.guide = guide
        with patch.object(threading, "Thread", Recorder):
            try:
                result = c._run_guide_session(SimpleNamespace())
            except RuntimeError as ex:
                result = ex
        return result, started

    def test_the_handover_starts_once_guide_is_sent(self):
        """Its settle wait sees PHD2's SettleDone whether or not StartGuiding ever comes."""
        unit = FakeUnit()
        c, _ = connector(unit)
        unit.active.add(UnitActivities.Acquiring)
        result, started = self.run(c, lambda imager_settings: None)
        assert result == CanonicalResponse_Ok
        assert started == [c.do_mirror_first_handover]

    def test_no_handover_thread_without_a_handover(self):
        unit = FakeUnit(auto_insert=False)
        c, _ = connector(unit)
        _, started = self.run(c, lambda imager_settings: None)
        assert started == []

    def test_a_refused_guide_request_lowers_both_flags(self):
        unit = FakeUnit()
        c, _ = connector(unit)
        result, started = self.run(c, lambda imager_settings: CanonicalResponse(errors=["not connected"]))
        assert isinstance(result, CanonicalResponse) and result.failed
        assert started == []
        assert not unit.active & {UnitActivities.PreGuiding, UnitActivities.Guiding}

    def test_a_guide_request_that_raises_lowers_both_flags(self):
        unit = FakeUnit()
        c, _ = connector(unit)

        def busy(imager_settings):
            raise RuntimeError("cannot guide while settling")

        result, started = self.run(c, busy)
        assert isinstance(result, RuntimeError)
        assert started == []
        assert not unit.active & {UnitActivities.PreGuiding, UnitActivities.Guiding}

    def test_an_unanswered_stop_still_lowers_both_flags(self):
        """A settle that timed out is likeliest when PHD2 has stopped answering."""
        unit = FakeUnit()
        c, _ = connector(unit, SettleResult(SettleOutcome.TIMED_OUT, 1.0))
        begin(c, unit, acquiring=True)

        def dead():
            raise RuntimeError("no connection to PHD2 server")

        c.stop_guiding = dead
        c.do_mirror_first_handover(c._guide_session)
        assert not unit.active & {UnitActivities.PreGuiding, UnitActivities.Guiding}
