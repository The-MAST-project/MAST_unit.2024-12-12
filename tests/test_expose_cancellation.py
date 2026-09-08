"""Aborting an exposure run stops it, rather than wedging it (#212).

Two mechanisms, each local to the component that owns it, and both needed:

- the **imager** releases its own waiters. `start_exposure` waits for the save inline, so
  that is where an exposure run actually parks -- `abort_exposure` has to set the events
  itself, because no readout is coming to set them.
- the **unit** stops its own loop, through `_expose_cancelled`. Deliberately not by
  clearing `UnitActivities.Exposing`: that flag stays up until `do_expose` has unwound, so
  `expose`'s one-run-at-a-time guard cannot open on a thread that is still closing its
  exposure series.

The second is the invariant worth stating twice, because it is what stops #212's
`ValueError: Cannot end exposure series` coming back through the front door.

What is **not** here: that `start_exposure` reports an aborted frame as failed rather than
returning as though it had been taken. Reaching that branch means standing up most of an
ASCOM backend, and the assertion a stub can make is not the one that matters -- whether the
real driver releases the wait at all is a question only the unit answers. It is a step in the
on-unit run instead.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from common.activities import ImagerActivities, UnitActivities
from unit import Unit

CADENCE_SECONDS = 30.0
FRAME_SECONDS = 5.0


class Recorder:
    """An imager that exposes instantly and remembers what it was asked to do."""

    def __init__(self):
        self.exposures = 0
        self.latest_settings = None

    def start_exposure(self, settings):
        self.exposures += 1

    def wait_for_image_saved(self):
        pass


class Mount:
    def __init__(self):
        self.offsets = []

    class _PW:
        def __init__(self, outer):
            self.outer = outer

        def mount_offset(self, **kwargs):
            self.outer.offsets.append(kwargs)

    @property
    def pw(self):
        return Mount._PW(self)

    def wait_until_settled(self, mode):
        pass


class Runner:
    """Enough of a Unit to run `_expose_repeatedly`'s own body."""

    def __init__(self):
        self.imager = Recorder()
        self.mount = Mount()
        self._expose_cancelled = threading.Event()


@pytest.fixture
def moved(monkeypatch):
    """The image paths `_expose_repeatedly` handed to the filer during one test."""
    import unit as unit_module

    paths: list[str] = []

    class Settings:
        def __init__(self, **kwargs):
            self.image_path = "ram/frame.fits"

    class Guardian:
        def protect(self, path):
            return _nullcontext()

    monkeypatch.setattr(unit_module, "ImagerSettings", Settings)
    monkeypatch.setattr(unit_module, "MoveGuardian", Guardian)
    monkeypatch.setattr(unit_module, "UnitRoi", lambda *a, **k: object())
    monkeypatch.setattr(unit_module, "ImagerRoi", types.SimpleNamespace(from_other=lambda roi: _Binnable()))
    monkeypatch.setattr(unit_module, "PathMaker", lambda: types.SimpleNamespace(make_exposures_folder=lambda: "folder"))
    monkeypatch.setattr(unit_module, "filer", types.SimpleNamespace(move_ram_to_shared=paths.append))
    return paths


class _Binnable:
    def binned(self, binning):
        return object()


def _nullcontext():
    from contextlib import nullcontext

    return nullcontext()


def _run(runner, repeats=1, cadence_seconds=0.0):
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
        cadence_seconds,
    )


class TestTheRunStopsWhenCancelled:
    def test_a_cancelled_run_exposes_nothing(self, moved):
        runner = Runner()
        runner._expose_cancelled.set()

        _run(runner, repeats=3)

        assert runner.imager.exposures == 0
        assert moved == []

    def test_a_cancelled_frame_is_not_moved_to_the_shared_store(self, moved):
        """An aborted exposure is released with no readout, so the file was never written
        and `move_ram_to_shared` would be the next exception."""
        runner = Runner()

        def cancel_instead_of_saving():
            runner._expose_cancelled.set()

        runner.imager.wait_for_image_saved = cancel_instead_of_saving

        _run(runner, repeats=3)

        assert runner.imager.exposures == 1, "the run must stop at the frame boundary"
        assert moved == [], "no frame was saved, so none may be moved"

    def test_cancelling_during_the_cadence_hold_returns_at_once(self, moved):
        runner = Runner()
        threading.Timer(0.2, runner._expose_cancelled.set).start()

        started = time.monotonic()
        _run(runner, repeats=2, cadence_seconds=CADENCE_SECONDS)
        elapsed = time.monotonic() - started

        assert elapsed < CADENCE_SECONDS / 2, "the hold must be interruptible, not a bare sleep"
        assert runner.imager.exposures == 1


class TestTheCadence:
    """`cadence_seconds` is start-to-start, and best-effort: an exposure that overruns it
    does not shorten the next one or skip it."""

    def test_no_cadence_means_no_hold(self, moved):
        runner = Runner()

        started = time.monotonic()
        _run(runner, repeats=3)

        assert time.monotonic() - started < 1.0
        assert runner.imager.exposures == 3

    def test_an_overrun_starts_the_next_exposure_without_holding(self, moved):
        runner = Runner()
        cadence, repeats = 0.3, 2
        overrun_seconds = cadence * 2

        runner.imager.wait_for_image_saved = lambda: time.sleep(overrun_seconds)

        started = time.monotonic()
        _run(runner, repeats=repeats, cadence_seconds=cadence)
        elapsed = time.monotonic() - started

        assert runner.imager.exposures == repeats
        # A hold on top of an exposure that already outran the period would add at least
        # one more period; the run is simply allowed to fall behind instead.
        assert elapsed < repeats * overrun_seconds + cadence


class TestAbortReachesTheRun:
    class Aborting:
        autofocuser = None
        guider = None
        components: list = []

        def __init__(self, exposing: bool):
            self.activities = {UnitActivities.Exposing} if exposing else set()
            self._expose_cancelled = threading.Event()

        def is_active(self, activity):
            return activity in self.activities

    def test_abort_cancels_a_run_in_flight(self):
        stub = self.Aborting(exposing=True)

        Unit.abort(stub)  # type: ignore[arg-type]

        assert stub._expose_cancelled.is_set()

    def test_abort_leaves_the_flag_for_the_thread_to_clear(self):
        """The invariant behind the private event: `Exposing` means "a run is in flight"
        until `do_expose` has closed its series and stopped tracking. Clearing it here
        would open `expose`'s guard on a thread that is still unwinding."""
        stub = self.Aborting(exposing=True)

        Unit.abort(stub)  # type: ignore[arg-type]

        assert stub.is_active(UnitActivities.Exposing), "abort must not clear the completion signal"

    def test_abort_with_no_run_in_flight_cancels_nothing(self):
        stub = self.Aborting(exposing=False)

        Unit.abort(stub)  # type: ignore[arg-type]

        assert not stub._expose_cancelled.is_set()


class TestTheImagerReleasesItsWaiters:
    """`abort_exposure` ends `ImagerActivities.Exposing`, but nothing was ever going to set
    the events a caller is parked on -- the readout it was waiting for is not coming."""

    class Backend:
        def __init__(self, saved: bool = False, read: bool = False):
            self.connected = True
            self.errors: list[str] = []
            self.image_was_saved = saved
            self.image_was_read = read
            self.image_saved_event = threading.Event()
            self.image_ready_event = threading.Event()
            self.parent_imager = None

        def is_active(self, activity):
            return activity is ImagerActivities.Exposing

    def test_abort_releases_both_events(self, monkeypatch):
        import imagers.ascom as ascom
        from common.canonical import CanonicalResponse

        monkeypatch.setattr(ascom, "ascom_run", lambda *a, **k: CanonicalResponse(value=False))
        backend = self.Backend()

        ascom.ASCOMImager.abort_exposure(backend)  # type: ignore[arg-type]

        assert backend.image_saved_event.is_set()
        assert backend.image_ready_event.is_set()
