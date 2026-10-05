"""Every way an autofocus run fails leaves the unit as a finished run would (#290, #291).

A run that does not solve returns the focuser to where it found it, stops tracking, and
ends its activities. On mast01 two other failures did none of that: the pre-sweep settle
timeout returned with `Autofocusing` still set (2026-10-04), and with ps3cli down the
thread died on a bare `Exception` from `PS3CLIClient.connect`, with both activity flags
set and the traceback only on stderr (2026-10-05).

An operator stop is deliberately not a failure: it leaves the focuser and tracking where
they are, as it always has.
"""

from __future__ import annotations

import socket

import pytest
import test_autofocus_retries
from test_autofocus_folder_reaches_share import _run
from test_autofocus_retries import ENTRY_POSITION, Unit, _solved, _unsolved

import autofocusing
import focus_analysis
from common.activities import UnitActivities
from focus_analysis import FocusAnalysisError

harness = test_autofocus_retries.harness

AUTOFOCUS_ACTIVITIES = (UnitActivities.Autofocusing, UnitActivities.AutofocusAnalysis)


def _cleaned_up(unit: Unit) -> bool:
    return not any(unit.is_active(a) for a in AUTOFOCUS_ACTIVITIES)


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestAnAnalyserThatIsNotRunning:
    def test_is_an_analyser_that_did_not_start(self):
        with pytest.raises(FocusAnalysisError) as raised:
            focus_analysis.analyze_focus_files(["FOCUS25000.fits"], timeout=1, port=_closed_port())

        assert raised.value.phase == "start"


class TestASettleTimeout:
    @pytest.fixture
    def stuck_stage(self, harness, monkeypatch):
        monkeypatch.setattr(autofocusing, "COMPONENTS_SETTLE_TIMEOUT_SECONDS", 0.0)
        unit = Unit(max_tries=1)
        unit.stage.is_moving = True
        return unit

    def test_ends_the_activity(self, harness, stuck_stage):
        _run(harness, stuck_stage, [])

        assert _cleaned_up(stuck_stage)

    def test_stops_tracking_and_leaves_the_focuser_alone(self, harness, stuck_stage):
        _run(harness, stuck_stage, [])

        assert stuck_stage.mount.stopped_tracking == 1
        assert stuck_stage.focuser.commanded == []


class TestAnAnalyserThatDidNotStart:
    def test_cleans_up_like_a_run_that_did_not_solve(self, harness):
        unit = Unit(max_tries=3)

        _run(harness, unit, [FocusAnalysisError("did not start", phase="start")])

        assert _cleaned_up(unit)
        assert unit.focuser.position == ENTRY_POSITION
        assert unit.mount.stopped_tracking == 1


class TestARunThatRaises:
    @pytest.mark.parametrize("where", ["sweep", "analysis"])
    def test_is_logged_and_cleaned_up_without_killing_the_thread(self, harness, where):
        unit = Unit(max_tries=3)
        outcomes: list = []
        if where == "sweep":

            def start_exposure(settings):
                raise ValueError("Cannot end exposure series")

            unit.imager.start_exposure = start_exposure
        else:
            outcomes = [Exception("Failed to connect to 127.0.0.1:8998")]

        _run(harness, unit, outcomes)

        assert _cleaned_up(unit)
        assert unit.focuser.position == ENTRY_POSITION
        assert unit.mount.stopped_tracking == 1
        assert unit.errors

    def test_before_the_entry_position_is_known_leaves_the_focuser_alone(self, harness):
        unit = Unit(max_tries=1)

        def move_to_preset(preset):
            raise RuntimeError("stage gone")

        unit.stage.move_to_preset = move_to_preset

        _run(harness, unit, [])

        assert _cleaned_up(unit)
        assert unit.focuser.commanded == []
        assert unit.mount.stopped_tracking == 1


class TestAnOperatorStopIsUnchanged:
    def test_leaves_the_focuser_and_tracking_as_they_are(self, harness):
        unit = Unit(max_tries=3)
        unit.imager.wait_for_image_saved = lambda: unit.end_activity(UnitActivities.Autofocusing)

        _run(harness, unit, [])

        assert unit.mount.stopped_tracking == 0
        assert unit.focuser.position != ENTRY_POSITION


class TestTheFinishedPathsAreUnchanged:
    def test_a_solved_run(self, harness):
        unit = Unit(max_tries=1)

        _run(harness, unit, [_solved(position=25024.6)])

        assert _cleaned_up(unit)
        assert unit.focuser.position == 25024
        assert unit.mount.stopped_tracking == 1

    def test_a_run_that_did_not_solve(self, harness):
        unit = Unit(max_tries=2)

        _run(harness, unit, [_unsolved(), _unsolved()])

        assert _cleaned_up(unit)
        assert unit.focuser.position == ENTRY_POSITION
        assert unit.mount.stopped_tracking == 1
