"""The run folder must actually leave the RAM disk.

Regression: the guard in ``move_to_shared`` compared a native Windows path from
``PathMaker`` ("D:\\MAST\\...") against ``Filer.ram.root`` ("D:/MAST/"), so
``startswith`` was always False and the move was silently skipped -- eight runs'
frames were found still on the volatile RAM disk on 2026-07-22.  The mover
itself was never at fault: ``Filer.move_ram_to_shared`` normalises with
``as_posix()`` before rewriting the root.

These pin the separator-agnostic guard without touching a real share: ``Filer``
is faked, so the tests assert *what would be moved*, not the move.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from calibration.phases import artifacts


@pytest.fixture
def fake_filer(monkeypatch):
    """A Filer whose roots use forward slashes, as the real one does."""
    filer = MagicMock()
    filer.ram = SimpleNamespace(root="D:/MAST/")
    filer.shared = SimpleNamespace(root="Z:/MAST/mast02/")
    monkeypatch.setattr(artifacts, "Filer", lambda: filer)
    return filer


@pytest.mark.parametrize(
    "folder",
    [
        pytest.param(r"D:\MAST\2026-07-22\Calibration\Focuser\0002", id="windows-separators"),
        pytest.param("D:/MAST/2026-07-22/Calibration/Focuser/0002", id="posix-separators"),
        pytest.param(r"D:\MAST/2026-07-22\Calibration/Focuser\0002", id="mixed-separators"),
    ],
)
def test_ram_folder_is_moved_whatever_the_separators(fake_filer, folder):
    """PathMaker hands back native Windows paths; the roots are posix. Both, and
    anything in between, must be recognised as being on the RAM disk."""
    artifacts.move_to_shared(folder)

    fake_filer.move_ram_to_shared.assert_called_once_with(folder)


@pytest.mark.parametrize(
    "folder",
    [
        pytest.param(r"C:\MAST\somewhere\else", id="local-disk"),
        pytest.param("Z:/MAST/mast02/already/shared", id="already-on-the-share"),
    ],
)
def test_folders_outside_the_ram_disk_are_left_alone(fake_filer, folder):
    artifacts.move_to_shared(folder)

    fake_filer.move_ram_to_shared.assert_not_called()


def test_no_ram_disk_configured_is_not_an_error(monkeypatch):
    filer = MagicMock()
    filer.ram = None
    monkeypatch.setattr(artifacts, "Filer", lambda: filer)

    artifacts.move_to_shared(r"D:\MAST\whatever")

    filer.move_ram_to_shared.assert_not_called()


def test_a_failing_mover_does_not_raise(fake_filer):
    """The frames are already on disk; a failed move must not fail the run."""
    fake_filer.move_ram_to_shared.side_effect = OSError("share unreachable")

    artifacts.move_to_shared(r"D:\MAST\2026-07-22\Calibration\Focuser\0002")


def test_no_folder_is_a_no_op(fake_filer):
    """Memory-imager runs have no folder at all."""
    artifacts.move_to_shared(None)

    fake_filer.move_ram_to_shared.assert_not_called()
