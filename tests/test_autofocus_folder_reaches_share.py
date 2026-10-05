"""Every autofocus try's folder is handed to the ram->shared mover, however the try ends (#272).

`D:` is a RAM disk, so a folder the mover is never given is lost on the next reboot,
and nothing reports it: the mover logs only failures. On 2026-09-14 six of the night's
ten folders were left behind, one for each way a try could end without a move:
tolerance rejected (None, NaN or above `max_tolerance`), stopped mid-sweep, or the
autofocus thread raising.

Exactly once, not at least once: a second move of the same folder finds its source
already gone and reports it at ERROR (MAST_common#117).
"""

from __future__ import annotations

import math
import types

import pytest
import test_autofocus_retries
from test_autofocus_retries import IMAGES, TICKS_PER_STEP, Unit, _solved, _unsolved

from autofocusing import Autofocuser
from common.activities import UnitActivities
from focus_analysis import FocusAnalysisError, PS3AutofocusStatus, PS3FocusAnalysisResult, PS3FocusSample

harness = test_autofocus_retries.harness


def _rejected(tolerance: float | None) -> PS3AutofocusStatus:
    return PS3AutofocusStatus(
        is_running=False,
        analysis_result=PS3FocusAnalysisResult(
            has_solution=True,
            best_focus_position=25024.6,
            best_focus_star_diameter=15.8,
            tolerance=tolerance,
            vcurve_a=0.002,
            vcurve_b=-100.0,
            vcurve_c=1.2e6,
        ),
    )


@pytest.fixture
def events(harness, monkeypatch):
    """Records moves and plots in the order they happen; runs threads inline."""
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        harness, "filer", types.SimpleNamespace(move_ram_to_shared=lambda path: recorded.append(("move", path)))
    )
    monkeypatch.setattr(
        harness,
        "plot_autofocus_analysis",
        lambda result, folder, pixel_scale, metric_label: recorded.append(("plot", folder)),
    )
    monkeypatch.setattr(harness, "Thread", lambda target, args=(), **kw: types.SimpleNamespace(start=lambda: target(*args)))
    return recorded


def _run(module, unit, outcomes: list):
    """One autofocus run. Each outcome is the analyser's status, or an exception it raises."""
    handed = iter(outcomes)

    def analyse(files, timeout=60):
        outcome = next(handed)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    module.analyze_focus_files = analyse
    Autofocuser(unit).do_start_autofocus(exposure=5, ticks_per_step=TICKS_PER_STEP, number_of_images=IMAGES)  # type: ignore[arg-type]


def _moves(events) -> list[str]:
    return [path for kind, path in events if kind == "move"]


def _folders(harness_tmp) -> list[str]:
    return sorted(str(p) for p in harness_tmp.iterdir() if p.name.startswith("autofocus-"))


class TestEveryEndingMovesTheFolderOnce:
    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param(_rejected(79.29), id="tolerance-above-max"),
            pytest.param(_rejected(math.nan), id="tolerance-nan"),
            pytest.param(_rejected(None), id="tolerance-none"),
            pytest.param(_unsolved(), id="no-solution"),
            pytest.param(PS3AutofocusStatus(is_running=False, analysis_result=None), id="empty-result"),
            pytest.param(FocusAnalysisError("did not finish", phase="finish"), id="analyser-did-not-finish"),
            pytest.param(FocusAnalysisError("did not start", phase="start"), id="analyser-did-not-start"),
        ],
    )
    def test_an_unaccepted_try(self, harness, events, tmp_path, outcome):
        unit = Unit(max_tries=1)

        _run(harness, unit, [outcome])

        assert _moves(events) == _folders(tmp_path)

    def test_a_solved_try_is_left_to_the_plotter(self, harness, events, tmp_path):
        """The plotter moves a solve's folder once vcurve.png is written; a move here would race it."""
        unit = Unit(max_tries=1)

        _run(harness, unit, [_solved()])

        [folder] = _folders(tmp_path)
        assert events == [("plot", folder)]

    def test_every_try_of_a_multi_try_run(self, harness, events, tmp_path):
        """The 2026-09-14 pattern: a rejection, then no solution, then a solve."""
        unit = Unit(max_tries=3)

        _run(harness, unit, [_rejected(79.29), _unsolved(), _solved()])

        rejected, unsolved, solved = _folders(tmp_path)
        assert events == [("move", rejected), ("move", unsolved), ("plot", solved)]

    def test_a_try_stopped_mid_sweep(self, harness, events, tmp_path):
        unit = Unit(max_tries=3)
        unit.imager.wait_for_image_saved = lambda: unit.end_activity(UnitActivities.Autofocusing)

        _run(harness, unit, [])

        assert _moves(events) == _folders(tmp_path)
        assert len(_moves(events)) == 1

    def test_a_try_whose_thread_raises(self, harness, events, tmp_path):
        """Folder 0002 on the night: the thread died on an exception mid-sweep."""
        unit = Unit(max_tries=3)

        def start_exposure(settings):
            raise ValueError("Cannot end exposure series")

        unit.imager.start_exposure = start_exposure

        _run(harness, unit, [])

        assert _moves(events) == _folders(tmp_path)
        assert len(_moves(events)) == 1

        assert _moves(events) == _folders(tmp_path)
        assert len(_moves(events)) == 1


class TestThePlotterMovesTheFolderItPlotted:
    def test_after_writing_the_plot(self, tmp_path, monkeypatch):
        import matplotlib

        matplotlib.use("Agg")
        import plotting

        folder = tmp_path / "Autofocus" / "0001"
        folder.mkdir(parents=True)
        written_when_moved: list[bool] = []
        monkeypatch.setattr(
            plotting,
            "filer",
            types.SimpleNamespace(
                move_ram_to_shared=lambda path: written_when_moved.append((folder / "vcurve.png").exists())
            ),
        )

        # D^2 = a(x - 25000)^2 + 15^2, sampled across the minimum.
        a, best, d_min = 0.002, 25000, 15.0
        positions = [best + TICKS_PER_STEP * (i - IMAGES // 2) for i in range(IMAGES)]
        result = PS3FocusAnalysisResult(
            has_solution=True,
            best_focus_position=best,
            best_focus_star_diameter=d_min,
            tolerance=10.0,
            vcurve_a=a,
            vcurve_b=-2 * a * best,
            vcurve_c=a * best**2 + d_min**2,
            focus_samples=[
                PS3FocusSample(
                    is_valid=True,
                    focus_position=p,
                    num_stars=12,
                    star_rms_diameter_pixels=math.sqrt(a * (p - best) ** 2 + d_min**2),
                )
                for p in positions
            ],
        )
        monkeypatch.setattr(plotting.plt, "show", lambda: None)
        plotting.plot_autofocus_analysis(result, str(folder))

        assert written_when_moved == [True]
