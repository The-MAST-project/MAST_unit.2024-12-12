"""`status.latest.exposure.frames`: each frame an `expose` run attempted, with its name, its
real window (`DATE-OBS`, `EXPOSURE`) and, for one that failed, why (#327).

The loop is run through `Unit._expose_repeatedly`'s own body against an imager that writes a
real FITS, as PHD2 does. PHD2's failure shape is a `wait_for_image_saved` that returns with
no file and no exception, so that is what the failing fake does.
"""

from __future__ import annotations

import threading
import types
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from astropy.io import fits

from common.canonical import CanonicalResponse
from common.models.latest import LatestExposure
from unit import Unit

FRAME_SECONDS = 5.0
#: As PHD2 writes it: UTC, to the millisecond, with no zone (fitsiowrap.h).
DATE_OBS = ["2026-10-29T20:00:00.125", "2026-10-29T20:00:07.250", "2026-10-29T20:00:14.375"]


class Imager:
    """Writes each frame as PHD2 does, unless told to fail it the way PHD2 fails."""

    def __init__(self):
        self.latest_settings: Any = None
        self.saved = 0
        #: Frame indexes that come back with no file and no exception.
        self.silently_failing: set[int] = set()
        self.refuse_next = False
        self.on_wait: Callable[[], None] | None = None
        #: The ASCOM and ZWO backends' key for the exposure time.
        self.exposure_key = "EXPOSURE"

    def start_exposure(self, settings):
        self.latest_settings = settings
        if self.refuse_next:
            return CanonicalResponse(errors=["PHD2 is not connected"])
        return None

    def wait_for_image_saved(self):
        index = self.saved
        self.saved += 1
        if self.on_wait is not None:
            self.on_wait()
            return
        if index in self.silently_failing:
            return
        header = fits.Header()
        header["DATE-OBS"] = DATE_OBS[index]
        header[self.exposure_key] = FRAME_SECONDS
        fits.PrimaryHDU(header=header).writeto(self.latest_settings.image_path)


class Mount:
    pw = types.SimpleNamespace(mount_offset=lambda **kwargs: None)

    def wait_until_settled(self, mode):
        pass


class Runner:
    def __init__(self):
        self.imager = Imager()
        self.mount = Mount()
        self._expose_cancelled = threading.Event()


@pytest.fixture
def moved(monkeypatch, tmp_path):
    """The frames `_expose_repeatedly` handed to the mover; frames are written under tmp_path."""
    import unit as unit_module

    paths: list[str] = []
    names = iter(range(100))

    class Settings:
        def __init__(self, **kwargs):
            self.image_path = (tmp_path / f"frame-{next(names):03d}.fits").as_posix()

    monkeypatch.setattr(unit_module, "ImagerSettings", Settings)
    monkeypatch.setattr(unit_module, "MoveGuardian", lambda: types.SimpleNamespace(protect=lambda path: nullcontext()))
    monkeypatch.setattr(unit_module, "UnitRoi", lambda *a, **k: object())
    monkeypatch.setattr(
        unit_module,
        "ImagerRoi",
        types.SimpleNamespace(from_other=lambda roi: types.SimpleNamespace(binned=lambda b: object())),
    )
    monkeypatch.setattr(unit_module, "PathMaker", lambda: types.SimpleNamespace(make_exposures_folder=lambda: str(tmp_path)))
    monkeypatch.setattr(unit_module, "filer", types.SimpleNamespace(move_ram_to_shared=paths.append))
    return paths


def run(runner: Runner, latest: LatestExposure, repeats: int) -> None:
    Unit._expose_repeatedly(
        runner,  # type: ignore[arg-type]
        repeats,
        FRAME_SECONDS,
        None,
        100,
        1,
        0,
        0,
        100,
        100,
        None,
        None,
        0.0,
        latest,
    )


def record(tmp_path: Path, repeats: int) -> LatestExposure:
    return LatestExposure(exposure_seconds=FRAME_SECONDS, repeats=repeats).begin(tmp_path)


def test_each_frame_is_reported_with_its_name_and_window(moved, tmp_path):
    latest = record(tmp_path, 3)

    run(Runner(), latest, repeats=3)

    assert [f.name for f in latest.frames] == ["frame-000.fits", "frame-001.fits", "frame-002.fits"]
    assert [f.date_obs for f in latest.frames] == [
        "2026-10-29T20:00:00.125000+00:00",
        "2026-10-29T20:00:07.250000+00:00",
        "2026-10-29T20:00:14.375000+00:00",
    ], "aware UTC, in the form `started` has"
    assert all(f.exposure_seconds == FRAME_SECONDS and f.error is None for f in latest.frames)
    assert latest.completed == 3 and latest.last_frame == f"{tmp_path.as_posix()}/frame-002.fits"
    assert len(moved) == 3


def test_a_frame_with_no_file_is_reported_failed_and_not_counted(moved, tmp_path):
    """PHD2's failure: `wait_for_image_saved` returns, and nothing was written."""
    runner = Runner()
    runner.imager.silently_failing = {1}
    latest = record(tmp_path, 3)

    run(runner, latest, repeats=3)

    assert [f.name for f in latest.frames] == ["frame-000.fits", "frame-001.fits", "frame-002.fits"]
    assert latest.frames[1].error is not None and latest.frames[1].date_obs is None
    assert latest.frames[2].date_obs == "2026-10-29T20:00:14.375000+00:00", "the others' entries do not shift"
    assert latest.completed == 2
    assert f"{tmp_path.as_posix()}/frame-001.fits" not in moved, "a frame that was not written is not moved"


def test_a_refused_start_leaves_an_entry_and_ends_the_run(moved, tmp_path):
    runner = Runner()
    runner.imager.refuse_next = True
    latest = record(tmp_path, 3)

    with pytest.raises(RuntimeError, match="refused the exposure"):
        run(runner, latest, repeats=3)

    assert len(latest.frames) == 1
    assert "PHD2 is not connected" in (latest.frames[0].error or "")
    assert moved == []


def test_an_aborted_frame_is_reported_as_aborted(moved, tmp_path):
    runner = Runner()
    runner.imager.on_wait = runner._expose_cancelled.set
    latest = record(tmp_path, 3)

    run(runner, latest, repeats=3)

    assert len(latest.frames) == 1
    assert latest.frames[0].error == "aborted"
    assert moved == []


def test_the_frames_outlive_the_run(moved, tmp_path):
    latest = record(tmp_path, 2)

    run(Runner(), latest, repeats=2)
    latest.finish(True)

    assert not latest.running and latest.completed == 2


def test_the_ascom_and_zwo_exposure_key_is_read_too(moved, tmp_path):
    runner = Runner()
    runner.imager.exposure_key = "EXPTIME"
    latest = record(tmp_path, 1)

    run(runner, latest, repeats=1)

    assert latest.frames[0].exposure_seconds == FRAME_SECONDS
