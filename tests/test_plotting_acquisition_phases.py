"""The combined acquisition plot takes whichever phases left corrections.

FCU v1 leaves `sky` and `spec`; FCU v2 leaves `spec-mirror-out` and `spec-mirror-in`. A list
of the two v1 names drew nothing for a v2 acquisition, and `has_corrections` could not find
one at all.
"""

from __future__ import annotations

import pytest

import plotting
from common.corrections import Correction, Corrections


def _write_phase(top, phase, start_second, tolerance=1.0):
    folder = top / phase
    folder.mkdir()
    corrections = Corrections(
        phase=phase,
        target_ra=1.0,
        target_dec=10.0,
        tolerance_ra=tolerance,
        tolerance_dec=tolerance,
        sequence=[Correction(time=f"2026-11-15T12:00:{start_second:02d}.000Z", ra_delta=5.0, dec_delta=-5.0)],
        last_delta=Correction(time=f"2026-11-15T12:00:{start_second + 1:02d}.000Z", ra_delta=0.1, dec_delta=0.1),
    )
    (folder / "corrections.json").write_text(corrections.model_dump_json(), encoding="utf-8")


@pytest.fixture
def combined(monkeypatch):
    """What `plot_acquisition_corrections` hands to `plot_phase_corrections`."""
    captured: dict = {}

    def capture(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(plotting, "plot_phase_corrections", capture)
    return captured


@pytest.mark.parametrize(
    "phases",
    [["sky", "spec"], ["spec-mirror-out", "spec-mirror-in"], ["spec"]],
    ids=["fcu-v1", "fcu-v2", "fcu-v1-skip-sky"],
)
def test_every_phase_that_left_corrections_is_plotted_in_the_order_it_ran(tmp_path, combined, phases):
    # Written in the reverse of the order they ran in, so the folder order is not the answer.
    for second, phase in reversed(list(enumerate(phases))):
        _write_phase(tmp_path, phase, start_second=second * 10)

    plotting.plot_acquisition_corrections(str(tmp_path))

    assert list(combined["tolerances"]) == phases
    assert len(combined["corrections"].sequence) == 2 * len(phases)
    assert len(combined["ends_of_phases"]) == len(phases)


def test_other_folders_are_not_phases(tmp_path, combined):
    _write_phase(tmp_path, "spec-mirror-out", 0)
    _write_phase(tmp_path, "spec-mirror-in", 10)
    (tmp_path / "guiding").mkdir()
    (tmp_path / "guiding" / "corrections.json").write_text("{}", encoding="utf-8")
    (tmp_path / "spec-mirror-in-extra-notes").mkdir()

    assert plotting.acquisition_phases(str(tmp_path)) == ["spec-mirror-in", "spec-mirror-out"]
    plotting.plot_acquisition_corrections(str(tmp_path))
    assert list(combined["tolerances"]) == ["spec-mirror-out", "spec-mirror-in"]


def test_an_acquisition_of_the_second_version_counts_as_having_corrections(tmp_path):
    _write_phase(tmp_path, "spec-mirror-in", 0)

    assert plotting.acquisition_phases(str(tmp_path)) == ["spec-mirror-in"]


def test_a_folder_that_does_not_exist_has_no_phases(tmp_path):
    assert plotting.acquisition_phases(str(tmp_path / "nope")) == []
