"""The flux-metering spiral walk, driven end to end without hardware.

What is checked here is that a run walks exactly the rings it was asked for and stops,
that the imager and the ThorCam take the exposure times they were each given, and that
the unit measures nothing: every frame is written and kept, and no flux, best cell or
correlation is computed.

The mount, the imager and the flux meter are all fakes. The flux meter is the one the
production code already accepts by injection; the other two are here.
"""

from __future__ import annotations

import itertools
import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common.models.statuses import ImagerRoi
from flux_metering.flux_meter import SimulatedFluxMeter
from flux_metering.spirals import SpiralsParams, SpiralsSession


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
    """Walks the spiral the fake generator dictates, and reports the cell it is on.

    `reports_offset=False` is a PWI4 older than 4.0.11b8, which has no `spiral_offset`.
    """

    def __init__(self, cells, reports_offset: bool = True):
        self._cells = list(cells)
        self._index = 0
        self.reports_offset = reports_offset
        self.settles = 0
        self.spiral_restarts = 0
        self.pw = SimpleNamespace(
            mount_spiral_offset_new=self._new,
            mount_spiral_offset_next=self._next,
            status=self._status,
        )

    def _new(self, x_step_arcsec, y_step_arcsec):
        self.spiral_restarts += 1
        self.commanded_steps = (x_step_arcsec, y_step_arcsec)
        self._index = 0

    def _next(self):
        self._index += 1

    def _status(self):
        if not self.reports_offset:
            return SimpleNamespace(mount=SimpleNamespace(spiral_offset=None))
        cell = self._cells[min(self._index, len(self._cells) - 1)]
        return SimpleNamespace(mount=SimpleNamespace(spiral_offset=SimpleNamespace(x=cell[0], y=cell[1])))

    def wait_until_settled(self, mode, **kwargs):
        self.settles += 1

    def status(self):
        # The full set the frame headers read. `_pointing` takes all four from ONE status
        # call, so a fake that carried only dec would make the pointing cards silently
        # absent rather than wrong -- and the tests would pass while proving nothing.
        return SimpleNamespace(
            dec_j2000_degs=41.0,
            ra_j2000_hours=13.5,
            ha_hours=0.25,
            lmst_hours=14.0,
        )


class FakeStage:
    """A folding-mirror stage that arrives after a given number of polls.

    `is_moving` is deliberately ALWAYS False -- that is the real stage's behaviour for up
    to one 2-second poll period after a move is commanded, because `move_to_preset` does
    not set it and only `ontimer` refreshes it. Any wait built on that flag falls straight
    through, which is what happened on 2026-09-02.
    """

    def __init__(self, polls_to_arrive: int = 3, arrives: bool = True):
        self._remaining = polls_to_arrive
        self._arrives = arrives
        self.moves: list = []
        self.is_moving = False
        self.position = 150000

    def at_preset(self, preset) -> bool:
        if not self._arrives:
            return False
        if self._remaining > 0:
            self._remaining -= 1
            return False
        self.position = 283350
        return True

    def move_to_preset(self, preset):
        self.moves.append(preset)


class FakeUnit:
    def __init__(self, mount, imager):
        self.mount = mount
        self.imager = imager
        self.pw = mount.pw
        self.hostname = "test-unit"
        self.unit_conf = SimpleNamespace(
            acquisition=SimpleNamespace(gain=170),
            imager=SimpleNamespace(pixel_scale_at_bin1=0.2616),
        )
        self.acquirer = object()
        self.stage = FakeStage()
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


def _make_session(folder, peak_cell=(2, -1), cells=200, spiral=None, reports_offset=True, **params):
    """A session wired to fakes, writing into `folder`. See the `session` fixture.

    `spiral` overrides the cells the fake mount walks, for a PWI4 whose order is not the
    square spiral the session expects.
    """
    mount = FakeMount(spiral if spiral is not None else square_spiral(cells), reports_offset=reports_offset)
    unit = FakeUnit(mount, FakeImager())
    meter_kwargs = {k: params.pop(k) for k in ("peak_counts", "sigma_cells") if k in params}
    meter = SimulatedFluxMeter(peak_cell=peak_cell, **meter_kwargs)
    s = SpiralsSession(unit, flux_meter=meter)  # type: ignore[arg-type]
    s.params = SpiralsParams(**params)
    s.state.folder = str(folder)
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


def _stub_the_mover(monkeypatch) -> None:
    """`move_ram_to_shared` is a no-op here: these frames are in a tmp directory that is
    under no configured root, and what is under test is the walk, not the mover."""
    import flux_metering.spirals as session_module

    monkeypatch.setattr(session_module.filer, "move_ram_to_shared", lambda *a, **k: None)


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A session wired to fakes, with the mover stubbed out."""
    _stub_the_mover(monkeypatch)

    def build(**params):
        return _make_session(tmp_path, **params)

    return build


def test_it_walks_exactly_the_requested_rings(session):
    """Every cell of the origin and rings 1..N, each once, in the order PWI4 walks them --
    and nothing beyond."""
    s, _unit, mount, _meter = session(rings=2)

    terminal = s._walk_spiral()

    assert terminal == "completed"
    assert len(s.steps) == 25
    assert [tuple(step.cell) for step in s.steps] == list(itertools.islice(square_spiral(100), 25))
    assert max(step.ring for step in s.steps) == 2


@pytest.mark.parametrize("rings", [1, 2, 3])
def test_the_step_count_is_the_cells_of_the_requested_rings(session, rings):
    s, _unit, _mount, _meter = session(rings=rings)

    s._walk_spiral()

    assert len(s.steps) == (2 * rings + 1) ** 2 == s.params.total_cells


def test_the_mount_is_not_stepped_onto_a_ring_that_will_not_be_exposed(session):
    """The count is checked before moving, so the walk ends ON the last cell of the last
    ring rather than one offset beyond it."""
    s, _unit, mount, _meter = session(rings=2)

    s._walk_spiral()

    assert mount._index == 24, "the mount was offset past the last cell"
    assert mount.settles == 24


def test_a_pwi4_that_reports_no_offset_still_stops_after_the_requested_rings(session):
    """Without `spiral_offset` the ring cannot be read, so the cell count ends the walk.
    The steps are recorded with no cell, rather than a guessed one."""
    s, _unit, _mount, _meter = session(rings=1, reports_offset=False)

    assert s._walk_spiral() == "completed"
    assert len(s.steps) == 9
    assert all(step.cell is None and step.ring is None for step in s.steps)


def test_a_cell_beyond_the_last_ring_ends_the_walk_without_exposing_it(session):
    """PWI4 owns the traversal. If it reports a cell outside the requested rings, that is
    the end of the walk even before the count runs out, and that cell is not exposed."""
    s, _unit, _mount, _meter = session(rings=1, spiral=[(0, 0), (1, 0), (2, 0), (1, 1)])

    assert s._walk_spiral() == "completed"
    assert [tuple(step.cell) for step in s.steps] == [(0, 0), (1, 0)]


def test_where_the_light_is_does_not_change_when_the_walk_stops(session):
    """There is no stopping rule any more: a peak at the origin, one far outside the
    search, and no peak at all give the same walk."""
    counts = set()
    for peak_cell in [(0, 0), (1, -1), (9, 9)]:
        s, _unit, _mount, _meter = session(rings=2, peak_cell=peak_cell)
        s._walk_spiral()
        counts.add(len(s.steps))

    assert counts == {25}


def test_nothing_is_measured(session):
    """The unit writes the frames and records where they were taken. Every quantity that
    used to come from analysing them -- the flux, the best cell, saturation, the
    correlation -- is left absent rather than computed."""
    s, _unit, _mount, _meter = session(rings=1, peak_cell=(1, 0), peak_counts=60000.0)

    s._walk_spiral()

    for step in s.steps:
        assert step.flux is None
        assert step.saturated is False
        for exposure in step.exposures:
            assert exposure.flux is None
            assert exposure.position_source is None
    assert s.state.best_flux is None
    assert s.state.best_cell is None
    assert s.state.saturated_frames == 0
    assert s.state.result is None


def test_the_session_no_longer_reaches_for_the_analysis(session):
    """A guard against the analysis creeping back in through an import."""
    import flux_metering.spirals as session_module

    for name in ("measure_frame", "measure_pair", "saturated_pixels", "resolve_center", "SATURATION_ADU"):
        assert not hasattr(session_module, name), name


def test_abort_stops_the_walk(session):
    s, _unit, _mount, _meter = session(peak_cell=(2, -1))
    s._stop.set()

    assert s._walk_spiral() == "aborted"
    assert s.steps == []


# ----------------------------------------------------------------- frame names --


@pytest.mark.parametrize(
    ("index", "expected"),
    [(0, (0, 1)), (1, (1, 1)), (8, (1, 8)), (9, (2, 1)), (24, (2, 16)), (25, (3, 1)), (360, (9, 72))],
)
def test_a_step_index_maps_to_its_ring_and_step_within_the_ring(index, expected):
    from flux_metering.spirals import ring_position

    assert ring_position(index) == expected


def test_the_names_carry_the_ring_and_the_step_within_it():
    from flux_metering.spirals import frame_names

    assert frame_names(0, 0) == ("step-0-001-00.fits", "flux-0-001-00.fits")
    assert frame_names(10, 2) == ("step-2-002-02.fits", "flux-2-002-02.fits")
    assert frame_names(360, 1) == ("step-9-072-01.fits", "flux-9-072-01.fits")


def test_the_named_ring_agrees_with_the_ring_pwi4_walks(session):
    """The names are worked out from the step counter, not read from PWI4, so this pins
    that the two agree on the square spiral -- and that the steps within each ring are
    numbered from 001 without a gap."""
    from flux_metering.spirals import ring_position

    s, _unit, _mount, _meter = session(rings=3)
    s._walk_spiral()

    within: dict[int, list[int]] = {}
    for step in s.steps:
        ring, k = ring_position(step.index)
        assert ring == step.ring, step
        within.setdefault(ring, []).append(k)
        assert step.exposures[0].imager_frame == f"step-{ring}-{k:03d}-00.fits"
        assert step.exposures[0].flux_frame == f"flux-{ring}-{k:03d}-00.fits"
    assert within == {0: [1], **{r: list(range(1, 8 * r + 1)) for r in (1, 2, 3)}}


def test_every_frame_name_is_unique_across_the_largest_run(session):
    from flux_metering.spirals import MAX_RINGS, frame_names

    total = (2 * MAX_RINGS + 1) ** 2
    names = {frame_names(i, n) for i in range(total) for n in range(3)}

    assert len(names) == total * 3


def test_both_frames_are_written_for_every_exposure(session, tmp_path):
    s, _unit, _mount, _meter = session(rings=1, number_of_frames=2)

    s._walk_spiral()

    for step in s.steps:
        for exposure in step.exposures:
            assert (tmp_path / exposure.imager_frame).exists(), exposure.imager_frame
            assert (tmp_path / exposure.flux_frame).exists(), exposure.flux_frame


def test_exposures_record_their_own_windows(session):
    """The two exposures are started together and each records its own start and end, so
    how much they overlap is checkable afterwards rather than assumed."""
    s, _unit, _mount, _meter = session(rings=1)

    s._walk_spiral()

    for step in s.steps:
        for exposure in step.exposures:
            assert exposure.imager_started_utc <= exposure.imager_ended_utc
            assert exposure.flux_started_utc <= exposure.flux_ended_utc


# ------------------------------------------------------------- exposure times --


def test_the_two_exposure_times_are_independent():
    params = SpiralsParams(step_seconds=5.0, flux_seconds=0.25)

    assert params.flux_exposure_us == 250_000
    assert params.step_seconds == 5.0


def test_the_imager_takes_the_step_exposure_and_the_thorcam_the_flux_exposure(session):
    s, unit, _mount, meter = session(rings=1, step_seconds=2.0, flux_seconds=0.25)

    s._open_meter()
    s._walk_spiral()

    assert unit.imager.latest_settings.seconds == 2.0
    assert meter.exposure_us == 250_000


def test_the_acquisition_takes_its_own_exposure(session):
    s, unit, _mount, _meter = session(step_seconds=2.0, flux_seconds=0.25, acquisition_seconds=7.0)
    unit.acquirer = FakeAcquirer(unit)

    s._acquire()

    assert unit.acquirer.requested["seconds"] == 7.0


# ------------------------------------------------------------- step sizes on the sky --


def test_the_ra_step_is_sized_for_the_declination(session):
    """PWI4 steps the RA coordinate, so the commanded step is x / cos(dec): the fake mount
    reports dec +41, where 0.5" on the sky needs about 0.663" of RA."""
    import math

    s, _unit, mount, _meter = session(rings=1, x_step_arcsec=0.5, y_step_arcsec=0.5)

    s._walk_spiral()

    x, y = mount.commanded_steps
    assert x == pytest.approx(0.5 / math.cos(math.radians(41.0)))
    assert y == 0.5


def test_offsets_are_recorded_on_the_sky(session):
    s, _unit, _mount, _meter = session(rings=1, x_step_arcsec=0.5, y_step_arcsec=0.25)

    s._walk_spiral()

    step = next(st for st in s.steps if tuple(st.cell) == (1, 1))
    assert step.offset_arcsec == (0.5, 0.25)


def test_the_requested_declination_is_used_when_the_mount_cannot_be_read(session):
    import math

    s, _unit, mount, _meter = session(rings=1, dec_j2000_degs=60.0)
    mount.status = lambda: (_ for _ in ()).throw(RuntimeError("no telemetry"))

    s._walk_spiral()

    assert mount.commanded_steps[0] == pytest.approx(0.5 / math.cos(math.radians(60.0)))


def test_no_declination_at_all_fails_rather_than_guessing(session):
    import flux_metering.spirals as session_module

    s, _unit, mount, _meter = session(rings=1)
    mount.status = lambda: (_ for _ in ()).throw(RuntimeError("no telemetry"))

    with pytest.raises(session_module.SpiralsError, match="declination"):
        s._walk_spiral()


def test_a_step_near_the_pole_is_refused(session):
    import flux_metering.spirals as session_module

    s, _unit, mount, _meter = session(rings=1)
    mount.status = lambda: SimpleNamespace(dec_j2000_degs=88.0)

    with pytest.raises(session_module.SpiralsError, match="refusing"):
        s._walk_spiral()
    assert mount.spiral_restarts == 0, "nothing may be commanded once the step is refused"


def test_the_result_records_the_commanded_ra_step(session, tmp_path, monkeypatch):
    import json
    import math

    import flux_metering.spirals as session_module

    monkeypatch.setattr(session_module.filer, "flush", lambda **kw: True)
    s, _unit, _mount, _meter = session(rings=1)

    s._finish(s._walk_spiral())

    document = json.loads((tmp_path / "result.json").read_text())
    assert document["dec_for_ra_step_degs"] == pytest.approx(41.0)
    assert document["ra_step_commanded_arcsec"] == pytest.approx(0.5 / math.cos(math.radians(41.0)))
    assert document["params"]["x_step_arcsec"] == 0.5


# --------------------------------------------------------------------- aborting --


class AbortingImager(FakeImager):
    """Raises the session's stop flag during exposure number `abort_on` (from 1), the way
    `Unit.abort` does. With `cut_short`, that exposure also returns without a frame, as
    the ASCOM imager does when its exposure is aborted."""

    def __init__(self, session_ref: list, abort_on: int, cut_short: bool):
        super().__init__()
        self.session_ref = session_ref
        self.abort_on = abort_on
        self.cut_short = cut_short

    def start_exposure(self, settings):
        if self.exposures + 1 == self.abort_on:
            self.exposures += 1
            self.session_ref[0]._stop.set()
            if self.cut_short:
                return SimpleNamespace(failed=False, errors=None)
            fits.PrimaryHDU(data=np.zeros(self.shape, dtype=np.uint16)).writeto(settings.image_path, overwrite=True)
            return SimpleNamespace(failed=False, errors=None)
        return super().start_exposure(settings)


def _with_aborting_imager(session, abort_on, cut_short, **params):
    ref: list = []
    s, unit, mount, meter = session(**params)
    unit.imager = AbortingImager(ref, abort_on=abort_on, cut_short=cut_short)
    ref.append(s)
    return s, unit


def test_an_abort_stops_the_step_between_pairs(session):
    """`Unit.abort` must not be followed by fresh exposures: the pair in flight finishes,
    and the step's remaining pairs are not started."""
    s, unit = _with_aborting_imager(session, abort_on=1, cut_short=False, rings=1, number_of_frames=3)

    assert s._walk_spiral() == "aborted"
    assert unit.imager.exposures == 1
    assert len(s.steps) == 1 and len(s.steps[0].exposures) == 1


def test_a_pair_cut_short_by_the_abort_is_discarded(session, tmp_path):
    """The imager returns without a frame; the pair is not recorded, and its flux frame is
    not written, rather than naming a step frame that does not exist."""
    s, unit = _with_aborting_imager(session, abort_on=2, cut_short=True, rings=1, number_of_frames=3)

    assert s._walk_spiral() == "aborted"
    assert [e.imager_frame for e in s.steps[0].exposures] == ["step-0-001-00.fits"]
    assert not (tmp_path / "step-0-001-01.fits").exists()
    assert not (tmp_path / "flux-0-001-01.fits").exists()


def test_a_step_with_no_saved_pair_is_not_recorded(session):
    s, unit = _with_aborting_imager(session, abort_on=1, cut_short=True, rings=1, number_of_frames=3)

    assert s._walk_spiral() == "aborted"
    assert s.steps == []


def test_a_frame_missing_without_an_abort_fails_the_run(session):
    """Only an abort explains a missing frame. Otherwise it is a fault, not a quiet skip."""
    import flux_metering.spirals as session_module

    s, unit, _mount, _meter = session(rings=1)
    unit.imager.start_exposure = lambda settings: SimpleNamespace(failed=False, errors=None)

    with pytest.raises(session_module.SpiralsError, match="without saving"):
        s._walk_spiral()


# ------------------------------------------------------------ result.json progress --


def test_result_json_is_rewritten_after_every_step(session, tmp_path, monkeypatch):
    """A run that dies part-way must leave a document describing every step it finished."""
    import json

    s, _unit, _mount, _meter = session(rings=1)
    seen: list[int] = []
    original = s._save_progress

    def recording():
        original()
        seen.append(len(json.loads((tmp_path / "result.json").read_text())["steps"]))

    monkeypatch.setattr(s, "_save_progress", recording)
    s._walk_spiral()

    assert seen == list(range(1, 10))
    document = json.loads((tmp_path / "result.json").read_text())
    assert document["terminal_state"] is None, "an in-progress document must not claim an ending"
    assert not (tmp_path / "result.json.partial").exists()


def test_result_json_reaches_the_share_only_at_the_end(session, tmp_path, monkeypatch):
    import flux_metering.spirals as session_module

    moved: list[str] = []
    monkeypatch.setattr(session_module.filer, "move_ram_to_shared", lambda path: moved.append(os.path.basename(path)))
    monkeypatch.setattr(session_module.filer, "flush", lambda **kw: True)
    s, _unit, _mount, _meter = session(rings=1)

    s._walk_spiral()
    assert "result.json" not in moved

    s._finish("completed")
    assert moved.count("result.json") == 1


# ------------------------------------------------------------------ the result --


def test_the_result_records_what_was_asked_for_and_returns_the_mount(session, tmp_path, monkeypatch):
    import json

    import flux_metering.spirals as session_module

    monkeypatch.setattr(session_module.filer, "flush", lambda **kw: True)
    s, _unit, mount, _meter = session(rings=1, step_seconds=2.0, flux_seconds=0.25)
    terminal = s._walk_spiral()
    restarts = mount.spiral_restarts

    s._finish(terminal)

    document = json.loads((tmp_path / "result.json").read_text())
    assert document["terminal_state"] == "completed"
    assert document["active"] is False
    assert document["params"]["rings"] == 1
    assert document["params"]["step_seconds"] == 2.0
    assert document["params"]["flux_seconds"] == 0.25
    assert document["total_cells"] == 9
    assert document["flux_exposure_us"] == 250_000
    assert document["pixel_scale_at_bin1"] == pytest.approx(0.2616)
    assert len(document["steps"]) == 9
    assert document["result"] is None
    for gone in ("max_rings", "patience_rings", "max_radius_arcsec", "usable_fraction", "seconds"):
        assert gone not in document["params"], gone
    assert mount.spiral_restarts == restarts, "the spiral is reset when the next one starts, not here"


def test_the_status_is_the_typed_model_and_carries_the_steps(session):
    """The status endpoint's contract: a bare typed model, never a dict or an envelope."""
    from common.models.statuses import FluxMeteringStatus

    s, _unit, _mount, _meter = session(rings=1)
    s._walk_spiral()

    status = s.status()

    assert isinstance(status, FluxMeteringStatus)
    assert len(status.steps) == len(s.steps)
    # Where every frame was taken is readable from the status alone.
    assert all(step.exposures for step in status.steps)


def test_every_step_takes_the_requested_number_of_frames(session):
    s, _unit, _mount, _meter = session(rings=1, number_of_frames=3)

    s._walk_spiral()

    assert all(len(step.exposures) == 3 for step in s.steps)
    assert s.state.frames == 3 * len(s.steps)


def test_a_stalled_mover_stops_the_run_instead_of_filling_the_disk(session, monkeypatch):
    """Frames are moved to the share as they are written, so the ram disk holds only the
    backlog -- until the share stalls. Then the run must stop while frames are still whole,
    rather than fill the disk and fail part-way through writing one."""
    import flux_metering.spirals as session_module

    s, _unit, _mount, _meter = session(rings=1)
    monkeypatch.setattr(session_module.filer, "flush", lambda **kw: False)
    monkeypatch.setattr(s, "_ram_disk_free_bytes", lambda: 100 * 1024**2)  # 100 MB left

    assert s._walk_spiral() == "disk_full"
    assert "not draining" in (s.state.last_error or "")


def test_a_transient_backlog_only_pauses_the_run(session, monkeypatch):
    """A share hiccup that the mover recovers from must not end a 40-minute run."""
    import flux_metering.spirals as session_module

    s, _unit, _mount, _meter = session(rings=1)
    readings = iter([100 * 1024**2])  # low once, then plenty

    def free_space():
        return next(readings, 50 * 1024**3)

    monkeypatch.setattr(session_module.filer, "flush", lambda **kw: True)
    monkeypatch.setattr(s, "_ram_disk_free_bytes", free_space)

    assert s._walk_spiral() == "completed"


def test_unreadable_free_space_does_not_stop_a_run(session, monkeypatch):
    """A run must be stopped by a real answer, never by the inability to ask."""
    s, _unit, _mount, _meter = session(rings=1)
    monkeypatch.setattr(s, "_ram_disk_free_bytes", lambda: None)

    assert s._walk_spiral() == "completed"


def test_the_coordinates_the_operator_actually_types_are_accepted():
    """`RA_PATTERN` accepts space-separated sexagesimal, so `float()` is not enough --
    which is how a real request with `03 08 10.142` blew up. `acquirer.py` carries a
    comment about having fixed this exact defect once already."""
    from common.parsers import parse_target

    ra, dec = parse_target("03 08 10.142", " +40 57 20.275")

    assert ra == pytest.approx(3 + 8 / 60 + 10.142 / 3600)
    assert dec == pytest.approx(40 + 57 / 60 + 20.275 / 3600)


@pytest.mark.parametrize(
    ("ra", "dec"),
    [
        ("12:30:45.123", "-45:30:00.123"),  # colon-separated
        ("12 30 45.123", "-45 30 00.123"),  # space-separated
        (12.5125, -45.5),  # already decimal
        ("12.5125", "-45.5"),  # decimal as text
    ],
)
def test_every_accepted_coordinate_form_parses(ra, dec):
    from common.parsers import parse_target

    parsed_ra, parsed_dec = parse_target(ra, dec)

    assert parsed_ra == pytest.approx(12.5125, abs=1e-4)
    assert parsed_dec == pytest.approx(-45.5, abs=1e-4)


def test_absent_coordinates_stay_absent():
    """None means 'take it from the mount', which only the acquirer can do."""
    from common.parsers import parse_target

    assert parse_target(None, None) == (None, None)


def test_an_ra_of_zero_is_a_coordinate_not_a_missing_value():
    """Truthiness would send RA 0h to the mount instead of using it."""
    from common.parsers import parse_target

    assert parse_target(0.0, 0.0) == (0.0, 0.0)


def test_a_bad_coordinate_raises_rather_than_being_guessed_at():
    from common.parsers import parse_target

    with pytest.raises(ValueError):
        parse_target("not a coordinate", None)


# --------------------------------------------------------------- frame headers --
#
# The ThorCam frames used to carry only the six mandatory structural keywords, so one
# separated from its run folder was anonymous -- and `flux-1-007-00.fits` is a name that
# repeats in every run on the share. These pin what each frame now says about itself.


@pytest.fixture(scope="module")
def walked(tmp_path_factory):
    """A finished walk whose products sit in a properly shaped run folder.

    The folder shape matters: RUNDATE and RUNSEQ are read back out of it, so a bare
    tmp_path would omit them and the test would prove nothing.

    Built once per distinct set of parameters for the whole module, and shared: the tests
    using this fixture only read its products back, and none writes to it.
    """
    mp = pytest.MonkeyPatch()
    _stub_the_mover(mp)
    walks: dict = {}

    def build(**params):
        key = tuple(sorted(params.items()))
        if key not in walks:
            folder = tmp_path_factory.mktemp("walked") / "2026-09-01" / "FluxMetering" / "0004"
            folder.mkdir(parents=True)
            s, _unit, _mount, _meter = _make_session(folder, **{"rings": 1, **params})
            s._walk_spiral()
            walks[key] = (s, folder)
        return walks[key]

    yield build
    mp.undo()


def _header(folder, name):
    return fits.getheader(str(folder / name))


def test_a_flux_frame_carries_the_settings_it_was_taken_with(walked):
    _s, folder = walked(flux_gain=7, flux_black_level=3)
    h = _header(folder, "flux-0-001-00.fits")

    assert h["INSTRUME"] == "SimulatedFluxMeter"  # the CAMERA, not the hostname
    assert h["TELESCOP"] == "test-unit"  # the hostname belongs here
    assert h["CAMSN"] == "simulated"
    assert h["GAIN"] == 7
    assert h["CREATOR"] == "MAST save_spirals"
    assert h["DATE-OBS"] and h["DATE-END"]
    assert "EXPOSURE" not in h  # EXPTIME only; not a third convention


def test_the_black_level_is_recorded_so_the_frame_can_be_re_reduced(walked):
    """An offline reduction needs the pedestal the camera was told, so it is in the frame
    rather than only in result.json."""
    _s, folder = walked(flux_black_level=5)
    assert _header(folder, "flux-0-001-00.fits")["BLKLEVEL"] == 5


def test_a_step_frame_locates_itself_in_its_run(walked):
    _s, folder = walked()
    h = _header(folder, "flux-0-001-00.fits")

    assert h["RUNDATE"] == "2026-09-01"
    assert h["RUNSEQ"] == "0004"
    assert h["STEPIDX"] == 0
    assert h["RING"] == 0
    # The one card tying this frame to the imager frame of the same exposure window; that
    # pairing otherwise exists only inside result.json.
    assert h["IMGFRAME"] == "step-0-001-00.fits"


def test_the_cards_describe_the_step_being_exposed_not_the_previous_one(walked):
    """`self.state` is advanced only AFTER a step's exposures finish, so cards read off it
    would be one step stale. They come from `_expose_step`'s arguments instead."""
    s, folder = walked()
    later = next(step for step in s.steps if step.index == 3)
    h = _header(folder, "flux-1-003-00.fits")

    assert h["STEPIDX"] == 3
    assert (h["NAMERING"], h["RINGSTEP"]) == (1, 3)
    assert (h["CELLX"], h["CELLY"]) == tuple(later.cell)
    assert h["RING"] == later.ring


def test_a_flux_frame_records_both_exposure_times(walked):
    """EXPTIME is the ThorCam's own; IMGEXPT is that of the imager frame named in IMGFRAME,
    which is no longer the same number."""
    _s, folder = walked(step_seconds=2.0, flux_seconds=0.25)
    h = _header(folder, "flux-0-001-00.fits")

    assert h["EXPTIME"] == pytest.approx(0.25)
    assert h["IMGEXPT"] == pytest.approx(2.0)


def test_a_step_frame_is_stamped_with_its_place_in_the_spiral(walked):
    """The imager writes its own header; the spiral cards are added after it, so a step
    frame identifies itself without its flux frame or result.json."""
    s, folder = walked()
    later = next(step for step in s.steps if step.index == 3)
    h = _header(folder, "step-1-003-00.fits")

    assert h["STEPIDX"] == 3
    assert (h["NAMERING"], h["RINGSTEP"]) == (1, 3)
    assert (h["CELLX"], h["CELLY"]) == tuple(later.cell)
    assert h["RING"] == later.ring
    assert h["OFFRA"] == pytest.approx(later.offset_arcsec[0])
    assert h["RUNDATE"] == "2026-09-01" and h["RUNSEQ"] == "0004"
    assert h["FLXFRAME"] == "flux-1-003-00.fits"


def test_stamping_adds_to_the_imager_header_rather_than_replacing_it(tmp_path):
    from flux_metering.spirals import stamp_header

    path = tmp_path / "frame.fits"
    header = fits.Header()
    header["EXPOSURE"] = (5.0, "written by the imager")
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16), header=header).writeto(path)

    stamp_header(str(path), [("STEPIDX", 7, "spiral step index"), ("CELLX", None, "unknown")])

    h = fits.getheader(str(path))
    assert h["EXPOSURE"] == 5.0
    assert h["STEPIDX"] == 7
    assert "CELLX" not in h, "an unknown value is omitted, not written empty"


def test_a_failed_stamp_does_not_fail_the_frame(tmp_path):
    from flux_metering.spirals import stamp_header

    stamp_header(str(tmp_path / "missing.fits"), [("STEPIDX", 1, "spiral step index")])


def test_the_pointing_is_recorded_in_degrees_and_named(walked):
    _s, folder = walked()
    h = _header(folder, "flux-0-001-00.fits")

    assert h["RA"] == pytest.approx(13.5 * 15)  # hours -> degrees, so nobody has to guess
    assert h["DEC"] == pytest.approx(41.0)
    assert h["EQUINOX"] == 2000.0
    assert h["RADESYS"] == "ICRS"
    assert h["HA"] == pytest.approx(0.25)
    assert h["LMST"] == pytest.approx(14.0)


def test_no_wcs_is_written(walked):
    """The ThorCam sees only the light out of the fibre and has no field. A missing card is
    an absence; a WCS would be an assertion that these pixels map to sky, and would invite a
    solver to solve it and DS9 to overlay catalogues on it."""
    _s, folder = walked()
    h = _header(folder, "flux-0-001-00.fits")

    for card in ("CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2", "CTYPE1", "CTYPE2", "CD1_1", "CDELT1"):
        assert card not in h


def test_an_unrequested_target_is_absent_rather_than_defaulted(walked):
    """On a skip_acquisition run no target was asked for. Absent, so a reader can tell that
    from a target that happened to be at the origin."""
    _s, folder = walked()
    assert "OBJECT" not in _header(folder, "flux-0-001-00.fits")


def test_a_requested_target_is_recorded(walked):
    _s, folder = walked(ra_j2000_hours=13.5, dec_j2000_degs=41.0)
    assert "13.5" in _header(folder, "flux-0-001-00.fits")["OBJECT"]


# ------------------------------------------------------------- the start guard --


def test_a_run_with_everything_present_is_allowed(session):
    """Guards the refusal tests below: if this returned a refusal they would all pass
    vacuously, on whatever reason happened to fire first."""
    s, _unit, _mount, _meter = session()
    assert s.require_can_start() is None


def test_a_run_without_a_stage_is_refused(session):
    """No stage means no folding mirror, and the fibre only sees light with the mirror in --
    so such a run would meter darkness and report it as a flux curve.

    A refusal string rather than an `assert`: asserts vanish under `python -O`, and the
    envelope renders AssertionError as an anonymous error the caller cannot act on.
    """
    s, unit, _mount, _meter = session()
    unit.stage = None

    refusal = s.require_can_start()

    assert refusal is not None
    assert "folding mirror" in refusal


def test_a_second_run_is_refused_while_one_is_in_progress(session):
    s, _unit, _mount, _meter = session()
    s._thread = SimpleNamespace(is_alive=lambda: True)
    assert "already in progress" in (s.require_can_start() or "")


def test_a_busy_unit_is_refused_and_says_what_it_is_doing(session):
    from common.activities import UnitActivities

    s, unit, _mount, _meter = session()
    unit.activities = UnitActivities.Guiding

    refusal = s.require_can_start()

    assert refusal is not None and "busy" in refusal


# ------------------------------------------------- positioning the folding mirror --


def test_it_waits_for_the_folding_mirror_rather_than_for_is_moving(session):
    """The 2026-09-02 regression.

    `Stage.is_moving` is a plain attribute refreshed by a 2-second poll, and
    `move_to_preset` does not set it -- so `while stage.is_moving` returns immediately and
    the run exposes with the mirror still travelling. `FakeStage.is_moving` is permanently
    False, so a wait built on it would return before `at_preset` was ever true.
    """
    s, unit, _mount, _meter = session()
    unit.stage = FakeStage(polls_to_arrive=4)

    assert s._position_folding_mirror() is True
    assert unit.stage.at_preset(None) is True  # it really did arrive
    assert unit.stage.moves, "the mirror was never commanded to SPEC"


def test_a_mirror_already_at_spec_is_not_commanded(session):
    s, unit, _mount, _meter = session()
    unit.stage = FakeStage(polls_to_arrive=0)

    assert s._position_folding_mirror() is True
    assert unit.stage.moves == [], "no move was needed"


def test_a_mirror_that_never_arrives_fails_the_run(session, monkeypatch):
    """Fails with a stated reason rather than exposing into a dark fibre."""
    import flux_metering.spirals as session_module

    monkeypatch.setattr(session_module, "STAGE_TIMEOUT_SECONDS", 0.5)
    s, unit, _mount, _meter = session()
    unit.stage = FakeStage(arrives=False)

    assert s._position_folding_mirror() is False
    assert "did not reach SPEC" in (s.state.last_error or "")


def test_positioning_is_visible_in_the_status(session):
    s, unit, _mount, _meter = session()
    unit.stage = FakeStage(polls_to_arrive=2)

    s._position_folding_mirror()

    assert s.state.phase == "positioning"


def test_an_abort_during_positioning_stops_the_run(session):
    s, unit, _mount, _meter = session()
    unit.stage = FakeStage(arrives=False)
    s._stop.set()

    assert s._position_folding_mirror() is False
    assert "aborted" in (s.state.last_error or "")


# ---------------------------------------------------------------- acquisition --


class FakeAcquirer:
    """Shaped like the real one: `run_acquisition` raises `Acquiring` only after a delay,
    and the endpoint spawns it on a thread and returns at once. The delay is the window in
    which "not started yet" used to read as "finished" (MAST_unit#316)."""

    START_DELAY_S = 0.3
    RUN_S = 0.3

    def __init__(self, unit, *, failure=None, refusal=None):
        self.unit = unit
        self.failure = failure
        self.refusal = refusal
        self.requested: dict = {}
        self.finished = False

    def prepare_acquisition(self, **kwargs):
        from common.canonical import CanonicalResponse

        self.requested = kwargs
        if self.refusal is not None:
            return CanonicalResponse(errors=[self.refusal])
        return SimpleNamespace(failure=None)

    def run_acquisition(self, acquisition):
        from common.activities import UnitActivities

        time.sleep(self.START_DELAY_S)
        self.unit.start_activity(UnitActivities.Acquiring)
        time.sleep(self.RUN_S)
        acquisition.failure = self.failure
        self.unit.end_activity(UnitActivities.Acquiring)
        self.finished = True

    def endpoint_start_acquisition_and_guiding(self, **kwargs):
        from threading import Thread

        from common.canonical import CanonicalResponse, CanonicalResponse_Ok

        prepared = self.prepare_acquisition(**kwargs)
        if isinstance(prepared, CanonicalResponse):
            return prepared
        Thread(target=self.run_acquisition, args=[prepared]).start()
        return CanonicalResponse_Ok


def test_the_run_waits_for_an_acquisition_that_starts_late(session):
    s, unit, _mount, _meter = session()
    unit.acquirer = FakeAcquirer(unit)

    assert s._acquire() is True
    assert unit.acquirer.finished, "the spiral must not start before the acquisition ends"


def test_an_acquisition_that_misses_tolerance_fails_the_run(session):
    s, unit, _mount, _meter = session()
    unit.acquirer = FakeAcquirer(unit, failure="phase 'SPEC' did not reach tolerance in 20 tries")

    assert s._acquire() is False
    assert "phase 'SPEC' did not reach tolerance in 20 tries" in (s.state.last_error or "")


def test_a_refused_acquisition_fails_the_run_with_the_reason(session):
    s, unit, _mount, _meter = session()
    unit.acquirer = FakeAcquirer(unit, refusal="no target coordinates were determined")

    assert s._acquire() is False
    assert "no target coordinates were determined" in (s.state.last_error or "")
    assert not unit.acquirer.finished


def test_the_acquisition_is_spec_only_and_hands_nothing_to_the_guider(session):
    s, unit, _mount, _meter = session()
    unit.acquirer = FakeAcquirer(unit)

    s._acquire()

    assert unit.acquirer.requested["skip_sky"] is True
    assert unit.acquirer.requested["handover_automatically_to_guider"] is False


def test_an_abort_during_acquisition_fails_the_run(session):
    s, unit, _mount, _meter = session()
    unit.acquirer = FakeAcquirer(unit)
    s._stop.set()

    assert s._acquire() is False
    assert "aborted" in (s.state.last_error or "")


def _run_recording_the_order(s, unit, *, leaves_mirror_at_spec: bool) -> list[str]:
    """Drive `do_acquire_and_save_spirals` with the walk stubbed, recording when the
    acquisition, the mirror and the spiral happen."""
    events: list[str] = []

    class OrderedAcquirer(FakeAcquirer):
        START_DELAY_S = 0.0
        RUN_S = 0.0

        def run_acquisition(self, acquisition):
            super().run_acquisition(acquisition)
            if leaves_mirror_at_spec:
                # The real acquirer's last act on FCU v2: the mirror goes back in.
                unit.stage._remaining = 0
            events.append("acquire")

    unit.acquirer = OrderedAcquirer(unit)
    stage_move = unit.stage.move_to_preset

    def recording_move(preset):
        events.append("mirror")
        stage_move(preset)

    unit.stage.move_to_preset = recording_move
    spiral_new = unit.mount.pw.mount_spiral_offset_new

    def recording_reset(x_step_arcsec, y_step_arcsec):
        events.append("reset")
        spiral_new(x_step_arcsec, y_step_arcsec)

    unit.mount.pw.mount_spiral_offset_new = recording_reset

    def walk():
        events.append("spiral")
        return "completed"

    s._walk_spiral = walk  # type: ignore[method-assign]
    s._finish = lambda terminal: events.append(f"finish:{terminal}")  # type: ignore[method-assign]

    s.do_acquire_and_save_spirals()
    return events


def test_the_mirror_is_not_moved_when_the_acquisition_leaves_it_in(session):
    """Putting the mirror in before acquiring was undone at once: the acquisition takes it
    out to solve and puts it back, two 42 s traverses for nothing (MAST_unit#312)."""
    s, unit, _mount, _meter = session()

    events = _run_recording_the_order(s, unit, leaves_mirror_at_spec=True)

    assert events == ["acquire", "spiral", "finish:completed"]
    assert unit.stage.moves == []


def test_a_mirror_the_acquisition_left_out_is_put_in_before_the_spiral(session):
    s, unit, _mount, _meter = session()

    events = _run_recording_the_order(s, unit, leaves_mirror_at_spec=False)

    assert events == ["acquire", "mirror", "spiral", "finish:completed"]


def test_a_run_without_acquisition_still_puts_the_mirror_in_first(session):
    s, unit, _mount, _meter = session(skip_acquisition=True)

    events = _run_recording_the_order(s, unit, leaves_mirror_at_spec=False)

    assert events == ["mirror", "spiral", "finish:completed"]


# ------------------------------- the simulator must model what it stands in for --
#
# These pin geometry, not behaviour. The simulator was built for a whole-frame sum, which
# needs no geometry at all: any frame size and any spot width give a usable number. Aperture
# photometry does not, and each of these was wrong before 2026-09-14.
#
# The run no longer measures anything, but `flux_meter.measure_frame` and the aperture
# photometry behind it are still in the tree for offline use, so their tests stay.


def test_the_simulator_frame_can_hold_the_extraction_aperture():
    """A 64x64 frame -- what this was -- cannot hold a 36 px aperture at all."""
    from flux_metering.aperture_photometry_single import DEFAULT_RADIUS
    from flux_metering.flux_meter import SimulatedFluxMeter

    ny, nx = SimulatedFluxMeter().expose().shape

    assert min(ny, nx) / 2 > DEFAULT_RADIUS, (
        f"a {DEFAULT_RADIUS:g} px aperture centred in a {nx}x{ny} frame falls off the edge"
    )


def test_the_simulator_models_the_real_fibre_width():
    """FWHM 12.7 px, which is what the fibre output measures on the real camera: 12.4-13.3 px
    across every frame of run 0006. The aperture radius is a fixed 36 px, so the spot width
    sets the enclosed-flux fraction -- a spot half the true width would make the simulator
    agree with the photometry for the wrong reason."""
    from flux_metering.flux_meter import SimulatedFluxMeter

    fwhm = 2.355 * SimulatedFluxMeter().spot_sigma_px

    assert 12.0 <= fwhm <= 13.5, f"simulated FWHM {fwhm:.1f} px is not the measured 12.4-13.3"


def test_the_simulator_does_not_report_false_saturation():
    """The aperture code counts pixels at or above 1022 -- the rail observed on the 10-bit
    Zelux. A 12-bit simulator peaking at 3000, which is what this was, reported hundreds of
    saturated pixels while sitting nowhere near its own full scale."""
    from flux_metering.aperture_photometry_single import SATURATION_ADU
    from flux_metering.flux_meter import SimulatedFluxMeter

    meter = SimulatedFluxMeter()
    frame = meter.expose()

    assert meter.saturation_level < 2 * SATURATION_ADU, "simulator full scale disagrees with the photometry's"
    assert int((frame >= SATURATION_ADU).sum()) == 0, "the default simulator must not look saturated"


def test_a_far_cell_gives_the_photometry_nothing_to_detect(session):
    """Not a defect -- the case the fallback exists for. Most of a spiral is far from the
    peak, where the fibre is dark, and the aperture code raises rather than returning zero."""
    import warnings

    import numpy as np

    from flux_metering.aperture_photometry_single import measure_single_image
    from flux_metering.flux_meter import SimulatedFluxMeter

    meter = SimulatedFluxMeter(peak_cell=(0, 0))
    meter.at_cell = (8, 0)

    with warnings.catch_warnings(), pytest.raises(RuntimeError):
        warnings.simplefilter("ignore")
        measure_single_image(np.asarray(meter.expose(), dtype=float), verbose=False)


# ------------------------------------------------ the aperture measurement itself --


def _dark_frame(shape=(256, 256), background=3.0):
    return np.full(shape, background, dtype=np.uint16)


def _spot_frame(x=100.0, y=140.0, peak=800.0, background=3.0, shape=(256, 256)):
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    data = background + peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2.0 * 5.4**2))
    return np.clip(data, 0, 1023).astype(np.uint16)


def test_a_detected_source_is_measured_where_it_actually_is():
    from flux_metering.flux_meter import measure_frame

    m = measure_frame(_spot_frame(x=100.0, y=140.0))

    assert m.position_source == "detected"
    assert m.x == pytest.approx(100.0, abs=1.0)
    assert m.y == pytest.approx(140.0, abs=1.0)
    assert m.net_counts > 0 and m.snr > 0


def test_the_aperture_is_redetected_per_frame():
    """Two frames with the source in different places must give two different centres --
    the measurement follows the source rather than a position fixed once."""
    from flux_metering.flux_meter import measure_frame

    a = measure_frame(_spot_frame(x=80.0, y=80.0))
    b = measure_frame(_spot_frame(x=170.0, y=120.0))

    assert a.x == pytest.approx(80.0, abs=1.0)
    assert b.x == pytest.approx(170.0, abs=1.0)


def test_a_frame_with_nothing_to_detect_falls_back_to_the_last_position():
    """Most of a spiral is far from the peak. The frame is still measured, through the same
    aperture as every other step, so the curve stays continuous and comparable."""
    from flux_metering.flux_meter import measure_frame

    m = measure_frame(_dark_frame(), last_position=(100.0, 140.0))

    assert m.position_source == "inherited"
    assert m.measured
    assert (m.x, m.y) == (100.0, 140.0)


def test_nothing_to_detect_and_no_previous_position_is_not_a_flux_of_zero():
    """The distinction the plain sum could not make. A frame that could not be measured and
    a frame containing no light give the same number under a sum, and the arg-max cannot
    tell them apart."""
    from flux_metering.flux_meter import measure_frame

    m = measure_frame(_dark_frame(), last_position=None)

    assert m.net_counts is None
    assert m.measured is False
    assert m.position_source == "none"
    assert m.error


def test_the_measurement_never_raises_on_a_hopeless_frame():
    from flux_metering.flux_meter import measure_frame

    for frame in (_dark_frame(), np.zeros((256, 256), dtype=np.uint16), _dark_frame(shape=(16, 16))):
        assert measure_frame(frame).measured in (True, False)  # the point is that it returns


def test_a_hot_pixel_far_from_the_source_does_not_enter_the_measurement():
    """It does under a whole-frame sum, at full weight."""
    from flux_metering.flux_meter import measure_frame

    clean = _spot_frame(x=100.0, y=140.0)
    fouled = clean.copy()
    fouled[20, 220] = 1023  # a corner, well outside a 36 px aperture on the source

    a, b = measure_frame(clean), measure_frame(fouled)

    assert b.net_counts == pytest.approx(a.net_counts, rel=1e-6)
    assert b.saturated_in_aperture == 0
    assert b.saturated_in_frame == 1, "but it is still visible as a whole-frame diagnostic"


def test_the_saturation_threshold_is_recorded_not_implied():
    from flux_metering.aperture_photometry_single import SATURATION_ADU
    from flux_metering.flux_meter import measure_frame

    assert measure_frame(_spot_frame()).saturation_threshold == SATURATION_ADU


def test_a_clipped_core_is_reported():
    """Fails against the old whole-frame count at 1023: this sensor rails at 1022, so
    `saturated_pixels(frame, 1023)` could never be true and argmax_saturated was dead."""
    from flux_metering.flux_meter import measure_frame

    m = measure_frame(_spot_frame(peak=5000.0))  # clips well before full scale

    assert m.saturated_in_aperture > 10, "a clipped core shows tens of pixels at the rail"


def _spike_frame(x=220, y=20, shape=(256, 256), background=3.0):
    """A hot pixel in an otherwise empty frame -- what run 0006's step 22 contained."""
    frame = np.full(shape, background, dtype=np.uint16)
    frame[y, x] = 1023
    return frame


def test_a_narrow_spike_is_not_accepted_as_the_fibre():
    """The defect real data found, and the reason the fallback cannot key on the backend
    raising. When segmentation finds nothing, `measure_single_image` falls back internally
    to a smoothed peak search and returns successfully -- with `detect_method` still reading
    'segment'. On run 0006 it locked onto a hot pixel at (943, 162), FWHM 1.1 px, and
    reported 1338 counts as though they were the fibre.
    """
    from flux_metering.flux_meter import measure_frame

    m = measure_frame(_spike_frame(), last_position=(100.0, 140.0))

    assert m.position_source == "inherited", "the spike must not be taken for the source"
    assert (m.x, m.y) == (100.0, 140.0)
    # Rejected on CONCENTRATION here, not width. On a noiseless frame `measure_fwhm` finds
    # no positive peak above the background and returns the configured guess of 11 px, which
    # clears any width floor -- so the width test alone would let this through. The real
    # 0006 frame, which has noise, is caught by width instead. Each covers the other's blind
    # spot, which is why both exist.
    assert m.error and "one pixel" in m.error


def test_a_rejected_spike_with_no_previous_position_is_unmeasurable():
    from flux_metering.flux_meter import measure_frame

    m = measure_frame(_spike_frame(), last_position=None)

    assert m.net_counts is None
    assert m.position_source == "none"
    assert "rejected a detection" in m.error


def test_a_real_source_is_well_clear_of_the_width_floor():
    """Guards the test above from passing by rejecting everything: the floor is 3 px and the
    fibre measures 12.4-13.3 on the real camera."""
    from flux_metering.flux_meter import MIN_PLAUSIBLE_FWHM_PX, measure_frame

    m = measure_frame(_spot_frame())

    assert m.position_source == "detected"
    assert m.fwhm_px > 2 * MIN_PLAUSIBLE_FWHM_PX


def test_the_width_floor_rejects_on_its_own():
    """The other discriminator, exercised directly.

    The concentration test above catches a spike on a clean frame; this one catches what
    real data produced. On run 0006's step 22 the backend's internal peak search found a hot
    pixel and reported FWHM 1.1 px -- noise there let `measure_fwhm` actually work, so the
    width is real and damning. Without this, removing the width check would leave every test
    passing.
    """
    from flux_metering.aperture_photometry_single import SPIKE_PEAK_FRACTION
    from flux_metering.flux_meter import _why_not_the_fibre

    data = np.full((256, 256), 3.0)
    found = {"fwhm_pix": 1.1, "x": 943.0, "y": 162.0, "net_counts": 1338.0, "radius_pix": 36.0, "bkg_level": 3.0}

    verdict = _why_not_the_fibre(data, found, SPIKE_PEAK_FRACTION)

    assert verdict and "too narrow" in verdict


def test_a_plausible_detection_is_not_rejected_by_either_test():
    """Guards both discriminators from passing by rejecting everything."""
    from flux_metering.aperture_photometry_single import SPIKE_PEAK_FRACTION
    from flux_metering.flux_meter import _why_not_the_fibre

    data = _spot_frame().astype(float)
    found = {"fwhm_pix": 12.7, "x": 100.0, "y": 140.0, "net_counts": 145000.0, "radius_pix": 36.0, "bkg_level": 3.0}

    assert _why_not_the_fibre(data, found, SPIKE_PEAK_FRACTION) is None


def test_the_exposure_model_carries_the_aperture_fields():
    """A paired-repo guard, not a formality.

    pydantic IGNORES unknown keyword arguments rather than raising, so if `common` lacks
    these fields the session would construct exposures that silently drop them and every
    other test here would still pass. That is exactly how the CD-matrix fields went onto the
    wrong SolvingSolution and took down every successful plate solve (MAST_common#113).
    """
    from common.models.statuses import FluxMeteringExposure

    e = FluxMeteringExposure(
        flux=None,
        position_source="detected",
        aperture_x=1.5,
        aperture_y=2.5,
        aperture_radius_px=36.0,
        counts_err=1.0,
        snr=2.0,
        bkg_level=2.6,
        fwhm_px=12.7,
        saturated_in_aperture=3,
        saturation_threshold=1022,
    )

    assert e.flux is None, "flux must accept None -- unmeasurable is not zero"
    assert e.position_source == "detected"
    assert e.aperture_x == 1.5
    assert e.saturation_threshold == 1022


# ------------------------------------------ living beside acquire_and_find_max_flux --


def test_has_run_is_false_until_a_run_happens(session):
    s, _unit, _mount, _meter = session()

    assert s.has_run is False


def test_it_is_refused_while_an_acquire_and_find_max_flux_run_holds_the_unit(session):
    """Both runs raise `UnitActivities.FluxMetering`, so the old run being active is visible
    here without this session knowing anything about the other."""
    from common.activities import UnitActivities

    s, unit, _mount, _meter = session()
    unit.activities = UnitActivities.FluxMetering

    refusal = s.require_can_start()

    assert refusal is not None and "busy" in refusal


def test_the_old_endpoint_is_unchanged_when_no_spirals_run_is_active():
    from unit import Unit

    started = []
    old = SimpleNamespace(require_can_start=lambda: None, start=lambda params: started.append(params) or SimpleNamespace())
    runner = SimpleNamespace(flux_metering=old, spirals=SimpleNamespace(is_active=False))

    response = Unit.endpoint_acquire_and_find_max_flux(runner)  # type: ignore[arg-type]

    assert not response.errors
    assert len(started) == 1


def test_both_routes_and_both_status_routes_exist():
    import inspect

    from unit import Unit

    source = inspect.getsource(Unit)
    for route in ("acquire_and_find_max_flux", "find_max_flux_status", "acquire_and_save_spirals", "save_spirals_status"):
        assert f'base_path + "/{route}"' in source, route
