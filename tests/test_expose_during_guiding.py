"""An exposure taken while a guide session exists must finish, and land where asked.

Three faults, all met on mast01 during the 2026-09-08 on-sky run, all ending in the same
place: a caller blocked for ever in `wait_for_image_saved`, `do_expose`'s `finally` never
running, and `UnitActivities.Exposing` left raised so MAST_unit#219's one-run guard
refused every later frame. One frame that cannot start cost all of them.

1. While **guiding**, PHD2 will not take a separate exposure -- `start_exposure` asks for
   `save_image` instead. That RPC ignores the `path` parameter, writes a temp file of its
   own and returns the name; nothing emits `SingleFrameComplete`, so nothing sets the
   event. Every exposure taken while guiding on this fleet has been written to a temp file
   and lost.

2. While **paused**, `_is_guiding` is False (it accepts only Guiding and LostLock), so the
   capture branch runs -- and it opened with `set_limit_frame`, which PHD2 refuses whenever
   a session exists at all: *"Cannot set the frame limit ROI while calibrating or
   guiding."* The refusal was caught and logged, and the method then fell through without
   ever calling `capture_single_frame`.

3. Either way the wait was unbounded.

Runs in the unit venv (Windows): the import chain is Windows-only today (`stage.py` uses
pyximc names at module level). Skips cleanly elsewhere.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

try:
    from phd2.phd2 import PHD2Connector, PHD2ConnectorError
except (ImportError, NameError) as ex:  # NameError: stage.py off-Windows
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from common.activities import ImagerActivities
from common.models.statuses import ImagerRoi, ImagerSettings

TEMP_FILE = r"C:\Users\mast\AppData\Local\phd2\sav7C03.tmp"


class FakeParent:
    """The activity bookkeeping half of the imager, over a set."""

    def __init__(self) -> None:
        self.activities: set = set()

    def start_activity(self, activity, **kwargs) -> None:
        self.activities.add(activity)

    def end_activity(self, activity, **kwargs) -> None:
        self.activities.discard(activity)

    def is_active(self, activity) -> bool:
        return activity in self.activities


@pytest.fixture(autouse=True)
def _profile_matches(monkeypatch):
    """`profile_binning` / `profile_bpp` are read-only properties over the live config.

    start_exposure refuses when the request disagrees with them, which is a different
    check from the one under test here, so they are pinned to what the settings ask for.
    """
    monkeypatch.setattr(PHD2Connector, "profile_binning", 1)
    monkeypatch.setattr(PHD2Connector, "profile_bpp", 16)


def make_connector(app_state: str, tmp_path=None) -> PHD2Connector:
    p = object.__new__(PHD2Connector)
    p._connected = True
    p.lock = threading.Lock()
    p.errors = []
    p.app_state = app_state
    p.image_was_saved = False
    p.image_saved_event = threading.Event()
    p.parent = FakeParent()
    p.call = MagicMock(return_value={"result": {"filename": TEMP_FILE}})
    return p


def settings_for(path, *, use_set_limit_frame: bool = True) -> ImagerSettings:
    return ImagerSettings(
        seconds=2.0,
        binning=1,
        gain=100,
        format="raw16",
        roi=ImagerRoi(x=520, y=0, width=7760, height=4812),
        image_path=str(path),
        use_set_limit_frame=use_set_limit_frame,
    )


def methods(p: PHD2Connector) -> list[str]:
    return [c.args[0] if c.args else c.kwargs.get("method") for c in p.call.call_args_list]


class TestWhileGuiding:
    def test_the_frame_lands_where_the_caller_asked(self, tmp_path):
        """PHD2 ignores `path`; the reply says where the file really went."""
        written = tmp_path / "phd2-temp.fits"
        written.write_bytes(b"a frame")
        wanted = tmp_path / "sub" / "wanted.fits"

        p = make_connector("Guiding")
        p.call = MagicMock(return_value={"result": {"filename": str(written)}})

        p.start_exposure(settings_for(wanted))

        assert wanted.read_bytes() == b"a frame"
        assert not written.exists(), "the temp file should have been moved, not copied"
        assert "save_image" in methods(p)

    def test_the_waiter_is_released(self, tmp_path):
        """Nothing emits SingleFrameComplete on this path, so start_exposure must."""
        written = tmp_path / "t.fits"
        written.write_bytes(b"x")
        p = make_connector("Guiding")
        p.call = MagicMock(return_value={"result": {"filename": str(written)}})

        p.start_exposure(settings_for(tmp_path / "out.fits"))

        assert p.image_was_saved
        assert p.image_saved_event.is_set()
        assert ImagerActivities.Exposing not in p.parent.activities
        assert ImagerActivities.Saving not in p.parent.activities

    def test_a_reply_without_a_filename_fails_rather_than_hangs(self, tmp_path):
        p = make_connector("Guiding")
        p.call = MagicMock(return_value={"result": {}})

        response = p.start_exposure(settings_for(tmp_path / "out.fits"))

        assert response.failed
        assert p.image_saved_event.is_set(), "a caller must not be left waiting"
        assert ImagerActivities.Exposing not in p.parent.activities


class TestWhilePaused:
    """`_is_guiding` is False when paused, so this takes the capture branch."""

    def test_the_limit_frame_rides_on_the_capture(self, tmp_path):
        p = make_connector("Paused")
        settings = settings_for(tmp_path / "out.fits")

        p.start_exposure(settings)

        assert "set_limit_frame" not in methods(p), "PHD2 refuses that while a session exists"
        params = p.call.call_args_list[-1].kwargs["params"]
        # The rect as `ImagerRoi` conditioned it -- the ZWO backend wants width % 8 and
        # height % 2, so the requested 520,0,7760,4812 travels as 527,1,7744,4808. The
        # old path conditioned it identically, by passing the same object to
        # `set_limit_frame`, so this is not a change in what PHD2 is told.
        roi = settings.roi
        assert params["limit_frame"] == [roi.x, roi.y, roi.width, roi.height]
        assert params["path"] == str(tmp_path / "out.fits")

    def test_a_refused_capture_does_not_strand_the_waiter(self, tmp_path):
        p = make_connector("Paused")
        p.call = MagicMock(side_effect=PHD2ConnectorError("cannot capture single frame when capture is current"))

        response = p.start_exposure(settings_for(tmp_path / "out.fits"))

        assert response.failed
        assert p.image_saved_event.is_set()
        assert ImagerActivities.Exposing not in p.parent.activities
        assert ImagerActivities.Saving not in p.parent.activities


class TestTheWaitIsBounded:
    def test_it_returns_rather_than_blocking_for_ever(self):
        p = make_connector("Stopped")
        p.image_was_saved = False
        p.need_to_reset_limit_frame = False

        p.wait_for_image_saved(timeout=0.05)

        assert p.errors, "a wait that timed out must say so"
        assert ImagerActivities.Exposing not in p.parent.activities

    def test_a_saved_image_does_not_wait_at_all(self):
        p = make_connector("Stopped")
        p.image_was_saved = True
        p.need_to_reset_limit_frame = False

        p.wait_for_image_saved(timeout=0.05)

        assert not p.errors
