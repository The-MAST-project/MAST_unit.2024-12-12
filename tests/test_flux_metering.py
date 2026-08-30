"""The flux-metering spiral walk, driven end to end without hardware.

What is actually being checked is the stopping rule, because it is the part most easily
got wrong and the part whose failure is silent: a spiral circles the origin, so flux rises
and falls on every ring, and a rule that keys on "it went up then came down" stops at the
first near-pass and reports a confident wrong answer. These tests put the peak several
cells out and assert the walk finds it rather than the first local rise.

The mount, the imager and the flux meter are all fakes. The flux meter is the one the
production code already accepts by injection; the other two are here.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common.models.statuses import ImagerRoi
from flux_metering.flux_meter import SimulatedFluxMeter
from flux_metering.session import FluxMeteringParams, FluxMeteringSession


def square_spiral(n: int):
    """The cells of a square spiral, in the order PWI4 walks them.

    (0,0), then round ring 1, then ring 2 ... Only the ORDER matters here: the production
    code derives the ring from the cell it is handed rather than assuming a sequence, so
    this fake is free to differ in detail from PWI4 without invalidating the test.
    """
    x = y = 0
    yield (x, y)
    step = 1
    while True:
        for dx, dy, count in ((1, 0, step), (0, 1, step), (-1, 0, step + 1), (0, -1, step + 1)):
            for _ in range(count):
                x, y = x + dx, y + dy
                yield (x, y)
                n -= 1
                if n <= 0:
                    return
        step += 2


class FakeImager:
    """Writes a small frame where the real one would write a 94 MB one."""

    def __init__(self, shape=(64, 64)):
        self.full_frame = ImagerRoi.verbatim(x=0, y=0, width=shape[1], height=shape[0])
        self.latest_settings = None
        self.shape = shape
        self.exposures = 0

    def start_exposure(self, settings):
        self.exposures += 1
        fits.PrimaryHDU(data=np.zeros(self.shape, dtype=np.uint16)).writeto(settings.image_path, overwrite=True)
        return SimpleNamespace(failed=False, errors=None)

    def wait_for_image_saved(self):
        return None


class FakeMount:
    """Walks the spiral the fake generator dictates, and reports the cell it is on."""

    def __init__(self, cells):
        self._cells = list(cells)
        self._index = 0
        self.settles = 0
        self.spiral_restarts = 0
        self.pw = SimpleNamespace(
            mount_spiral_offset_new=self._new,
            mount_spiral_offset_next=self._next,
            status=self._status,
        )

    def _new(self, x_step_arcsec, y_step_arcsec):
        self.spiral_restarts += 1
        self._index = 0

    def _next(self):
        self._index += 1

    def _status(self):
        cell = self._cells[min(self._index, len(self._cells) - 1)]
        return SimpleNamespace(mount=SimpleNamespace(spiral_offset=SimpleNamespace(x=cell[0], y=cell[1])))

    def wait_until_settled(self, mode, **kwargs):
        self.settles += 1

    def status(self):
        return SimpleNamespace(dec_j2000_degs=41.0)


class FakeUnit:
    def __init__(self, mount, imager):
        self.mount = mount
        self.imager = imager
        self.pw = mount.pw
        self.hostname = "test-unit"
        self.unit_conf = SimpleNamespace(acquisition=SimpleNamespace(gain=170))
        self.acquirer = object()
        self.activities = 0
        self.activities_verbal = []
        self.started: list = []
        self.ended: list = []

    def is_active(self, flag):
        return bool(self.activities & flag)

    def start_activity(self, flag, **kwargs):
        self.activities |= flag
        self.started.append(flag)

    def end_activity(self, flag, **kwargs):
        self.activities &= ~flag
        self.ended.append(flag)


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A session wired to fakes, with the mover stubbed out.

    `move_ram_to_shared` is a no-op here: these frames are in a tmp directory that is under
    no configured root, and what is under test is the walk, not the mover.
    """
    import flux_metering.session as session_module

    monkeypatch.setattr(session_module.filer, "move_ram_to_shared", lambda *a, **k: None)

    def build(peak_cell=(2, -1), cells=200, **params):
        mount = FakeMount(square_spiral(cells))
        unit = FakeUnit(mount, FakeImager())
        meter_kwargs = {k: params.pop(k) for k in ("peak_counts", "sigma_cells") if k in params}
        meter = SimulatedFluxMeter(peak_cell=peak_cell, **meter_kwargs)
        s = FluxMeteringSession(unit, flux_meter=meter)  # type: ignore[arg-type]
        s.params = FluxMeteringParams(**params)
        s.state.folder = str(tmp_path)
        s._meter = meter
        # The simulator's reading follows wherever the mount says it is.
        original = s._read_spiral_offset

        def tracking_read():
            cell, ring, offset = original()
            if cell is not None:
                meter.at_cell = cell
            return cell, ring, offset

        s._read_spiral_offset = tracking_read  # type: ignore[method-assign]
        return s, unit, mount, meter

    return build


def test_it_finds_a_peak_several_cells_out(session):
    """The rule must not stop at the first rise-then-fall, which a spiral produces on every
    ring long before it reaches an off-centre peak."""
    s, _unit, _mount, _meter = session(peak_cell=(2, -1))

    terminal = s._walk_spiral()

    assert terminal == "converged"
    best = max(s.steps, key=lambda step: step.flux)
    assert best.cell == (2, -1)
    assert s.state.best_cell == (2, -1)


def test_it_walks_past_ring_one_to_get_there(session):
    """The specific failure the ring rule exists to prevent: ring 1 contains a local rise
    and fall, so a step-wise rule would stop inside it."""
    s, _unit, _mount, _meter = session(peak_cell=(3, 2))

    s._walk_spiral()

    best = max(s.steps, key=lambda step: step.flux)
    assert best.cell == (3, 2)
    assert best.ring == 3
    assert max(step.ring or 0 for step in s.steps) >= 4, "must complete a ring beyond the peak"


def test_a_centred_fibre_converges_at_the_origin(session):
    """The likely outcome for an already-calibrated unit, and the one that later
    short-circuits the correlation rather than correlating a frame with itself."""
    s, _unit, _mount, _meter = session(peak_cell=(0, 0))

    s._walk_spiral()

    assert max(s.steps, key=lambda step: step.flux).cell == (0, 0)


def test_max_rings_stops_a_search_that_will_not_converge(session):
    """A peak outside the search bound must end as `max_rings`, not as `converged` -- the
    two mean different things and only one says the arg-max is a peak."""
    s, _unit, _mount, _meter = session(peak_cell=(9, 9), sigma_cells=6.0, max_rings=2, cells=400)

    terminal = s._walk_spiral()

    assert terminal in ("max_rings", "max_radius")


def test_abort_stops_the_walk(session):
    s, _unit, _mount, _meter = session(peak_cell=(2, -1))
    s._stop.set()

    assert s._walk_spiral() == "aborted"
    assert s.steps == []


def test_saturation_is_recorded_and_does_not_stop_the_run(session):
    """Saturation is an observation, never a control action -- the run finishes and the
    result says whether the arg-max frame was clipped."""
    s, _unit, _mount, _meter = session(peak_cell=(1, 1), peak_counts=60000.0)

    terminal = s._walk_spiral()

    assert terminal == "converged"
    assert s.state.saturated_frames > 0
    assert any(step.saturated for step in s.steps)


def test_both_frames_are_written_for_every_step(session, tmp_path):
    s, _unit, _mount, _meter = session(peak_cell=(1, 0))

    s._walk_spiral()

    for step in s.steps:
        assert (tmp_path / step.imager_frame).exists(), step.imager_frame
        assert (tmp_path / step.flux_frame).exists(), step.flux_frame


def test_exposures_overlap_so_the_windows_can_be_checked(session):
    """The two exposures are taken in parallel and each records its own start and end, so
    the overlap is verifiable afterwards rather than assumed."""
    s, _unit, _mount, _meter = session(peak_cell=(1, 0))

    s._walk_spiral()

    for step in s.steps:
        assert step.imager_started_utc <= step.imager_ended_utc
        assert step.flux_started_utc <= step.flux_ended_utc


def test_the_flux_exposure_follows_the_imager_exposure():
    assert FluxMeteringParams(seconds=5.0).flux_exposure_us == 5_000_000
    assert FluxMeteringParams(seconds=0.25).flux_exposure_us == 250_000


def test_the_radius_cap_carries_cos_dec(session):
    """`x_step_arcsec` is RA COORDINATE arcsec, so the sky angle along it is scaled by
    cos(dec). Without this the cap means a different thing at every declination."""
    s, _unit, _mount, _meter = session()

    # The fake mount reports dec +41, where cos(dec) is about 0.755.
    assert s._radius_arcsec((10.0, 0.0)) == pytest.approx(10.0 * np.cos(np.radians(41.0)), rel=1e-6)
    assert s._radius_arcsec((0.0, 10.0)) == pytest.approx(10.0)
