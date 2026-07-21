"""Every calibration phase gets its own fresh, sequenced run folder.

Two failures this replaces:

* the focus phase wrote into ``Autofocus/<NNNN>`` -- the tree the operational
  ps3cli/PWI4 autofocus also uses -- so a night running both interleaved two
  tools in one numbering with nothing in the path to tell them apart;
* the stage phase could not run at all: the orchestrator never passed a folder,
  and a file-only imager (PHD2) fails its first precondition without one.
"""

import re
from pathlib import Path

import pytest

from common.paths import CALIBRATION_PHASE_FOLDERS, PathMaker


@pytest.fixture
def root(tmp_path):
    return str(tmp_path)


@pytest.mark.parametrize(
    "phase, expected_folder",
    [
        pytest.param("focuser", "Focuser", id="focuser"),
        pytest.param("optical_center", "OpticalCenter", id="optical_center-is-CamelCase"),
        pytest.param("stage", "Stage", id="stage"),
    ],
)
def test_phase_folder_is_capitalised(root, phase, expected_folder):
    """Folders follow the tree's Capitalised convention.

    ``optical_center`` is the one that cannot be derived mechanically --
    capitalising it would give ``Optical_center`` -- so the mapping is explicit.
    """
    made = Path(PathMaker().make_calibration_folder(phase, root=root))

    assert made.parent.name == expected_folder
    assert made.parent.parent.name == "Calibration"
    assert re.fullmatch(r"\d{4}", made.name), "run folder is a 4-digit sequence"
    assert made.is_dir()


def test_each_run_gets_a_fresh_folder(root):
    """Per-run isolation is what keeps frame names unique across re-runs."""
    first = PathMaker().make_calibration_folder("focuser", root=root)
    second = PathMaker().make_calibration_folder("focuser", root=root)

    assert first != second
    assert Path(first).name == "0001"
    assert Path(second).name == "0002"


def test_sequences_are_per_phase(root):
    """Re-running one phase repeatedly -- the commissioning pattern -- must not
    have its numbering advanced by unrelated phases."""
    PathMaker().make_calibration_folder("focuser", root=root)
    PathMaker().make_calibration_folder("focuser", root=root)
    stage = PathMaker().make_calibration_folder("stage", root=root)
    focuser = PathMaker().make_calibration_folder("focuser", root=root)

    assert Path(stage).name == "0001", "stage has its own counter"
    assert Path(focuser).name == "0003"


def test_calibration_is_separate_from_operational_autofocus(root):
    """The collision this was built to prevent: calibration frames must not land
    in the tree the ps3cli/PWI4 autofocus writes to."""
    calibration = Path(PathMaker().make_calibration_folder("focuser", root=root))
    autofocus = Path(PathMaker().make_autofocus_folder(root=root))

    assert "Autofocus" not in calibration.parts
    assert "Calibration" not in autofocus.parts


def test_unknown_phase_is_rejected(root):
    """The mapping is a whitelist -- an arbitrary string must not become a path."""
    with pytest.raises(ValueError, match="unknown calibration phase"):
        PathMaker().make_calibration_folder("../escape", root=root)

    assert set(CALIBRATION_PHASE_FOLDERS) == {"focuser", "optical_center", "stage"}
