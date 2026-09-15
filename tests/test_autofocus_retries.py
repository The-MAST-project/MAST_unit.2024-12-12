"""Each autofocus try is an independent measurement (#233).

The retry loop had three ways of carrying one try's state into the next, and on
2026-09-14 they combined into a run that ended 750 ticks from the last known-good
focus with nothing recording that it had:

- the sweep's start position was computed **once, before** the loop and only ever
  incremented, so try 1 continued upward from wherever try 0 stopped rather than
  re-centering. By try 2 the sweep sampled nothing but defocus, which is a fit that
  cannot succeed however good the optics are.
- the exposure series was **opened once and closed per try**, so tries after the
  first ran against a series already ended, and the backend's end hook re-ran once
  per try. That is inert today, because `Imager.start_exposure_series` never calls
  the backend's *start* hook at all and every end hook is therefore a no-op (#240).
  It is the phd2 end hook that would resume guiding, so wiring up the start hook --
  the obvious fix for that dead code -- is what turns this into a retry exposing
  down `save_image` at the guide profile's settings.
- the give-up message was keyed on ``try_number == max_tries - 1``, which is also
  true of a run that *solved* on its final try.

The invariant the tests below are really defending: **what try N does must not
depend on what try N-1 did.** A retry is a fresh attempt at the same measurement,
not a continuation of a single marching sweep.

Not here: the acceptance criterion that rejected good solutions on the night
(#237), and the unconditional ``stop_tracking()`` on the exit path (#232). Both
are separate tickets and neither is exercised by these fakes.
"""

from __future__ import annotations

import types
from contextlib import nullcontext

import pytest

from autofocusing import Autofocuser
from common.activities import UnitActivities
from common.config.rois import SkyRoiConfig
from focus_analysis import PS3AutofocusStatus, PS3FocusAnalysisResult

KNOWN_AS_GOOD = 25000
ENTRY_POSITION = 24800
IMAGES = 5
TICKS_PER_STEP = 50
MAX_TOLERANCE = 60

# start_position - (images / 2) * ticks_per_step
FIRST_SAMPLE = int(KNOWN_AS_GOOD - (IMAGES / 2) * TICKS_PER_STEP)


class Focuser:
    """Records every commanded position; never busy, so no test waits on a move."""

    def __init__(self, position: int = ENTRY_POSITION):
        self._position = position
        self.commanded: list[int] = []

    @property
    def position(self) -> int:
        return self._position

    @position.setter
    def position(self, value: int) -> None:
        self._position = int(value)
        self.commanded.append(self._position)

    def is_active(self, _activity) -> bool:
        return False


class Imager:
    """Remembers the exposure-series lifecycle and where the focuser was per frame."""

    def __init__(self, focuser: Focuser):
        self.focuser = focuser
        self.series_starts = 0
        self.series_ends = 0
        self.open = False
        self.positions_exposed: list[int] = []
        self.exposures_with_no_series_open = 0

    def start_exposure_series(self, purpose: str | None = None):
        self.series_starts += 1
        self.open = True
        return types.SimpleNamespace(series_id=f"series-{self.series_starts}", purpose=purpose)

    def end_exposure_series(self, series) -> None:
        self.series_ends += 1
        self.open = False

    def start_exposure(self, settings) -> None:
        if not self.open:
            self.exposures_with_no_series_open += 1
        self.positions_exposed.append(self.focuser.position)

    def wait_for_image_saved(self) -> None:
        pass


class Mount:
    is_moving = False

    def __init__(self):
        self.stopped_tracking = 0

    def stop_tracking(self) -> None:
        self.stopped_tracking += 1

    def goto_ra_dec_j2000(self, ra, dec) -> None:  # pragma: no cover - not reached
        raise AssertionError("no target is passed by these tests")


class Stage:
    is_moving = False

    def move_to_preset(self, preset) -> None:
        pass


class Unit:
    """Enough of a Unit for `do_start_autofocus` to run end to end."""

    hostname = "mast-test"
    fcu_version = "v2"
    connected = True

    def __init__(self, max_tries: int):
        self.focuser = Focuser()
        self.imager = Imager(self.focuser)
        self.mount = Mount()
        self.stage = Stage()
        self.errors: list[str] = []
        self.activities: set = set()
        self.pw = types.SimpleNamespace(status=lambda: types.SimpleNamespace(mount=types.SimpleNamespace(is_tracking=True)))
        self.unit_conf = types.SimpleNamespace(
            focuser=types.SimpleNamespace(known_as_good_position=KNOWN_AS_GOOD),
            acquisition=types.SimpleNamespace(
                rois={"v2": SkyRoiConfig(sky_x=5054, sky_y=2721, width=3000, height=3000)},
                binning=1,
                gain=170,
            ),
            autofocus=types.SimpleNamespace(max_tries=max_tries, max_tolerance=MAX_TOLERANCE),
            imager=types.SimpleNamespace(pixel_scale_at_bin1=0.262),
        )

    def start_activity(self, activity, details=None) -> None:
        self.activities.add(activity)

    def end_activity(self, activity) -> None:
        self.activities.discard(activity)

    def is_active(self, activity) -> bool:
        return activity in self.activities


def _solved(position: float = 25024.6) -> PS3AutofocusStatus:
    return PS3AutofocusStatus(
        is_running=False,
        analysis_result=PS3FocusAnalysisResult(
            has_solution=True,
            best_focus_position=position,
            best_focus_star_diameter=15.0,
            tolerance=10.0,
            vcurve_a=0.002,
            vcurve_b=-100.0,
            vcurve_c=1.2e6,
        ),
    )


def _unsolved() -> PS3AutofocusStatus:
    return PS3AutofocusStatus(
        is_running=False,
        analysis_result=PS3FocusAnalysisResult(
            has_solution=False,
            best_focus_position=None,
            best_focus_star_diameter=None,
            tolerance=None,
            vcurve_a=None,
            vcurve_b=None,
            vcurve_c=None,
        ),
    )


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Patches out everything `do_start_autofocus` touches that is not the loop."""
    import autofocusing as module

    folders = []

    def make_folder():
        folder = tmp_path / f"autofocus-{len(folders):04d}"
        folder.mkdir()
        folders.append(folder)
        return str(folder)

    monkeypatch.setattr(module, "PathMaker", lambda: types.SimpleNamespace(make_autofocus_folder=make_folder))
    monkeypatch.setattr(module, "UnitRoi", lambda *a, **k: object())
    monkeypatch.setattr(module, "ImagerRoi", types.SimpleNamespace(from_other=lambda roi: object()))
    monkeypatch.setattr(module, "ImagerSettings", lambda **kw: types.SimpleNamespace(**kw))
    monkeypatch.setattr(module, "filer", types.SimpleNamespace(move_ram_to_shared=lambda path: None))
    monkeypatch.setattr(module, "MoveGuardian", lambda: types.SimpleNamespace(protect=lambda *a: nullcontext()))
    monkeypatch.setattr(module, "Config", lambda: types.SimpleNamespace(update_unit=lambda fn, unit_name=None: None))
    monkeypatch.setattr(module, "Thread", lambda **kw: types.SimpleNamespace(start=lambda: None))
    return module


def _run(module, unit, results: list[PS3AutofocusStatus]):
    """Drive one autofocus run whose analyser returns `results`, one per try."""
    handed = iter(results)
    module_results = []

    def analyse(files, timeout=60):
        status = next(handed)
        module_results.append(status)
        return status

    import autofocusing

    autofocusing.analyze_focus_files = analyse  # type: ignore[assignment]
    focuser = Autofocuser(unit)  # type: ignore[arg-type]
    focuser.do_start_autofocus(exposure=5, ticks_per_step=TICKS_PER_STEP, number_of_images=IMAGES)
    return focuser


class TestEveryTryStartsFromTheSamePlace:
    def test_each_try_recenters_on_the_start_position(self, harness, monkeypatch):
        """The night's failure: tries 1 and 2 continued upward instead of re-centering."""
        unit = Unit(max_tries=3)

        _run(harness, unit, [_unsolved(), _unsolved(), _unsolved()])

        exposed = unit.imager.positions_exposed
        assert len(exposed) == 3 * IMAGES
        first_of_each_try = [exposed[i * IMAGES] for i in range(3)]
        assert first_of_each_try == [FIRST_SAMPLE, FIRST_SAMPLE, FIRST_SAMPLE]

    def test_a_try_sweeps_the_configured_span(self, harness):
        unit = Unit(max_tries=1)

        _run(harness, unit, [_unsolved()])

        assert unit.imager.positions_exposed == [FIRST_SAMPLE + i * TICKS_PER_STEP for i in range(IMAGES)]


class TestTheExposureSeriesIsPairedWithTheTry:
    def test_every_try_opens_and_closes_its_own_series(self, harness):
        unit = Unit(max_tries=3)

        _run(harness, unit, [_unsolved(), _unsolved(), _unsolved()])

        assert unit.imager.series_starts == 3
        assert unit.imager.series_ends == 3
        assert not unit.imager.open

    def test_no_frame_is_taken_outside_a_series(self, harness):
        """Structural today, load-bearing the moment the backend start hook is wired up
        (#240): the phd2 end hook resumes guiding, and a frame taken after it would go
        down `save_image` at the guide profile's exposure and gain."""
        unit = Unit(max_tries=3)

        _run(harness, unit, [_unsolved(), _unsolved(), _unsolved()])

        assert unit.imager.exposures_with_no_series_open == 0


class TestGivingUpIsReportedOnlyWhenItHappens:
    def test_a_success_on_the_last_try_is_not_reported_as_a_failure(self, harness):
        unit = Unit(max_tries=3)

        _run(harness, unit, [_unsolved(), _unsolved(), _solved()])

        assert not [e for e in unit.errors if "could not achieve" in e]

    def test_a_single_try_that_succeeds_is_not_reported_as_a_failure(self, harness):
        """`max_tries = 1` made `try_number == max_tries - 1` true of every outcome."""
        unit = Unit(max_tries=1)

        _run(harness, unit, [_solved()])

        assert not [e for e in unit.errors if "could not achieve" in e]

    def test_exhausting_the_tries_is_still_reported(self, harness):
        unit = Unit(max_tries=3)

        _run(harness, unit, [_unsolved(), _unsolved(), _unsolved()])

        assert [e for e in unit.errors if "could not achieve" in e]


class TestAFailedRunLeavesNoTrace:
    def test_the_focuser_returns_to_where_the_run_found_it(self, harness):
        unit = Unit(max_tries=3)

        _run(harness, unit, [_unsolved(), _unsolved(), _unsolved()])

        assert unit.focuser.position == ENTRY_POSITION

    def test_a_solved_run_leaves_the_focuser_at_best_focus(self, harness):
        unit = Unit(max_tries=1)

        _run(harness, unit, [_solved(position=25024.6)])

        assert unit.focuser.position == 25024


class TestTheActivityIsAlwaysDropped:
    def test_autofocusing_ends_whatever_the_outcome(self, harness):
        unit = Unit(max_tries=2)

        _run(harness, unit, [_unsolved(), _unsolved()])

        assert not unit.is_active(UnitActivities.Autofocusing)
