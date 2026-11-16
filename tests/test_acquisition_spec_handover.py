"""FCU v2 solves at SPEC twice -- mirror out, then mirror in -- and guides from the second.

The first solve is taken with the folding mirror parked at the `Min` preset, the second after
the mirror has arrived at SPEC and settled. Each is a phase of its own (`spec-mirror-out`,
`spec-mirror-in`), so each keeps its own folder of frames, solver results and corrections;
only the second is the reference. FCU v1 keeps its single `spec` phase.

Needs no hardware: every component is a stub that records what it was asked to do.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from test_acquisition_slew_wait import _acquisition, _in_tmp_path, _Unit  # noqa: F401  (fixture)

import acquirer as acquirer_module
from acquirer import ACQUISITION_CANCELLED, SPEC_MIRROR_IN, SPEC_MIRROR_OUT, Acquirer
from common.canonical import CanonicalResponse
from common.config.rois import FcuVersion


class _Stage:
    is_moving = False

    def __init__(self, events, refuse=None):
        self.events, self.refuse, self.position = events, refuse, 0

    def move_to_preset(self, preset):
        self.events.append(f"stage->{preset.value}")
        if self.refuse == preset.value:
            return CanonicalResponse(errors=["out of range"])
        self.position = {"min": 100, "spec": 321297, "sky": 190000}[preset.value]
        return CanonicalResponse(value="ok")


class _Solver:
    def __init__(self, events, stage, results):
        self.events, self.stage, self.results = events, stage, results
        self.calls: list[dict] = []

    def solve_and_correct(self, **kwargs):
        phase = kwargs["phase"]
        self.events.append(f"solve:{phase}")
        self.calls.append({**kwargs, "stage_position": self.stage.position})
        return self.results.get(phase, True)


class _Guider:
    _backend = None

    def __init__(self):
        self.base_folders: list[str] = []

    def make_guiding_settings(self, base_folder):
        self.base_folders.append(base_folder)
        return SimpleNamespace(tags={})


class _Imager:
    connected = True
    can_image_to_memory = True
    _backend = None

    def __init__(self, events):
        self.events = events

    @property
    def image_array(self):
        # What guiding will compare against: whichever solve came last when this is read.
        return [event for event in self.events if event.startswith("solve:")][-1]

    def start_exposure_series(self, purpose):
        return "series"

    def end_exposure_series(self, series):
        self.events.append("end-series")


def _acquire(fcu_version, monkeypatch, results=None, refuse=None, skip_sky=False):
    unit = _Unit(fcu_version)
    unit.stage = unit.required_stage = _Stage(unit.events, refuse)
    unit.solver = unit.required_solver = _Solver(unit.events, unit.stage, results or {})
    unit.guider = unit.required_guider = _Guider()
    unit.imager = unit.required_imager = _Imager(unit.events)

    a = object.__new__(Acquirer)
    a.unit = unit
    unit.acquirer = unit.required_acquirer = a
    monkeypatch.setattr(a, "_cancellable_sleep", lambda seconds: True)
    monkeypatch.setattr(acquirer_module, "time", SimpleNamespace(monotonic=unit.clock.monotonic, sleep=lambda s: None))

    acquisition = _acquisition(skip_sky=skip_sky)
    acquisition.handover_automatically_to_guider = False
    acquisition.folder = "acq"
    acquisition.post_process = lambda: None
    return unit, Acquirer.do_acquire(a, acquisition)


def test_v2_parks_at_min_solves_moves_to_spec_and_solves_again(monkeypatch):
    unit, failure = _acquire(FcuVersion.v2, monkeypatch)

    assert failure is None
    stage_and_solves = [e for e in unit.events if e.startswith(("stage->", "solve:"))]
    assert stage_and_solves == [
        "stage->min",
        f"solve:{SPEC_MIRROR_OUT}",
        "stage->spec",
        f"solve:{SPEC_MIRROR_IN}",
    ]


def test_v2_never_parks_at_sky(monkeypatch):
    unit, _ = _acquire(FcuVersion.v2, monkeypatch)

    assert "stage->sky" not in unit.events


def test_each_solve_is_taken_with_the_mirror_where_its_phase_says(monkeypatch):
    unit, _ = _acquire(FcuVersion.v2, monkeypatch)

    positions = {call["phase"]: call["stage_position"] for call in unit.solver.calls}
    assert positions == {SPEC_MIRROR_OUT: 100, SPEC_MIRROR_IN: 321297}


def test_each_phase_has_its_own_folder_and_tags_its_frames_with_the_stage_position(monkeypatch):
    unit, _ = _acquire(FcuVersion.v2, monkeypatch)

    assert unit.guider.base_folders == [os.path.join("acq", SPEC_MIRROR_OUT), os.path.join("acq", SPEC_MIRROR_IN)]
    tags = [call["imager_settings"].tags for call in unit.solver.calls]
    assert tags == [{"stage": 100}, {"stage": 321297}]


def test_the_reference_is_the_mirror_in_solve_only(monkeypatch):
    unit, _ = _acquire(FcuVersion.v2, monkeypatch)

    assert unit.reference_image == f"solve:{SPEC_MIRROR_IN}"
    # Taken once, after the second solve: the first one was never the reference.
    assert unit.events.index("end-series") > unit.events.index(f"solve:{SPEC_MIRROR_IN}")


def test_a_failed_mirror_out_solve_does_not_move_the_mirror_to_spec(monkeypatch):
    unit, failure = _acquire(FcuVersion.v2, monkeypatch, results={SPEC_MIRROR_OUT: False})

    assert failure is not None
    assert "stage->spec" not in unit.events
    assert f"solve:{SPEC_MIRROR_IN}" not in unit.events


def test_a_failed_mirror_in_solve_fails_the_acquisition_without_a_reference(monkeypatch):
    unit, failure = _acquire(FcuVersion.v2, monkeypatch, results={SPEC_MIRROR_IN: False})

    assert failure is not None
    assert SPEC_MIRROR_IN.upper() in failure
    assert unit.reference_image is None
    assert unit.activities == set()


@pytest.mark.parametrize("refused", ["min", "spec"])
def test_a_refused_stage_move_fails_the_acquisition_instead_of_solving_in_the_wrong_place(refused, monkeypatch):
    unit, failure = _acquire(FcuVersion.v2, monkeypatch, refuse=refused)

    assert failure is not None
    assert f"cannot move the stage to '{refused}'" in failure
    assert ACQUISITION_CANCELLED not in failure
    assert unit.activities == set()
    solved = [e for e in unit.events if e.startswith("solve:")]
    assert solved == ([] if refused == "min" else [f"solve:{SPEC_MIRROR_OUT}"])


def test_v1_keeps_its_single_spec_phase(monkeypatch):
    unit, failure = _acquire(FcuVersion.v1, monkeypatch, skip_sky=True)

    assert failure is None
    assert [e for e in unit.events if e.startswith("solve:")] == ["solve:spec"]
    assert "stage->min" not in unit.events
    assert unit.events.count("stage->spec") == 1
