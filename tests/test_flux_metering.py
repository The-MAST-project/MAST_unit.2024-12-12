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
import time
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
        self.unit_conf = SimpleNamespace(acquisition=SimpleNamespace(gain=170))
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
        for exposure in step.exposures:
            assert (tmp_path / exposure.imager_frame).exists(), exposure.imager_frame
            assert (tmp_path / exposure.flux_frame).exists(), exposure.flux_frame
        # The step points at the pair the correlation would use.
        assert step.imager_frame == step.exposures[step.representative].imager_frame


def test_exposures_overlap_so_the_windows_can_be_checked(session):
    """The two exposures are taken in parallel and each records its own start and end, so
    the overlap is verifiable afterwards rather than assumed."""
    s, _unit, _mount, _meter = session(peak_cell=(1, 0))

    s._walk_spiral()

    for step in s.steps:
        for exposure in step.exposures:
            assert exposure.imager_started_utc <= exposure.imager_ended_utc
            assert exposure.flux_started_utc <= exposure.flux_ended_utc


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


def test_the_status_is_the_typed_model_and_carries_the_steps(session):
    """`FullUnitStatus.flux_metering` is typed as this model, so the session must hand back
    the model itself -- a dict would be accepted by nothing downstream."""
    from common.models.statuses import FluxMeteringStatus

    s, _unit, _mount, _meter = session(peak_cell=(1, 0))
    s._walk_spiral()

    status = s.status()

    assert isinstance(status, FluxMeteringStatus)
    assert len(status.steps) == len(s.steps)
    assert status.best_cell == (1, 0)
    # The flux curve is plottable from the status alone, without fetching the products.
    assert all(step.flux > 0 for step in status.steps)


def test_it_nests_in_the_unit_status_without_an_envelope(session):
    """The endpoint contract's one load-bearing exception: a status returns its bare model,
    because `FullUnitStatus` types its fields as the status models and an envelope nested in
    the payload would break every consumer silently."""
    from common.models.statuses import FluxMeteringStatus, FullUnitStatus

    s, _unit, _mount, _meter = session(peak_cell=(1, 0))
    s._walk_spiral()

    full = FullUnitStatus(id=1, flux_metering=s.status())
    round_tripped = FullUnitStatus.model_validate(full.model_dump())

    assert isinstance(round_tripped.flux_metering, FluxMeteringStatus)
    assert round_tripped.flux_metering.best_cell == (1, 0)
    assert len(round_tripped.flux_metering.steps) == len(s.steps)


def test_flux_metering_is_absent_from_the_status_until_a_run_happens(session):
    """A unit that never meters flux pays nothing for the field."""
    s, _unit, _mount, _meter = session()

    assert s.has_run is False


def test_the_step_flux_is_the_median_and_names_the_nearest_frame():
    """Median, not mean: the arg-max is decided where the coupling curve is flattest, which
    is exactly where one outlier has most leverage over which cell wins."""
    flux, nearest = FluxMeteringSession.representative_of([100.0, 101.0, 5000.0])

    assert flux == 101.0, "a single wild sample must not move the step's flux"
    assert nearest == 1, "and the frame chosen must be the one that produced the median"


def test_an_odd_burst_picks_an_actual_sample():
    """With an odd count the median IS a sample, so the chosen pair is exact rather than
    nearest to an interpolated value -- the reason odd counts are preferred."""
    fluxes = [30.0, 10.0, 20.0]

    flux, nearest = FluxMeteringSession.representative_of(fluxes)

    assert flux == 20.0
    assert fluxes[nearest] == flux


def test_an_even_burst_still_resolves_to_one_frame():
    """Even counts are allowed and must not be ambiguous, even though the median is then
    interpolated and belongs to no frame."""
    flux, nearest = FluxMeteringSession.representative_of([10.0, 20.0, 30.0, 100.0])

    assert flux == 25.0
    assert nearest in (1, 2)


def test_every_step_takes_the_requested_number_of_frames(session):
    s, _unit, _mount, _meter = session(peak_cell=(1, 0), number_of_frames=3)

    s._walk_spiral()

    assert all(len(step.exposures) == 3 for step in s.steps)
    assert s.state.frames == 3 * len(s.steps)


def test_a_stalled_mover_stops_the_run_instead_of_filling_the_disk(session, monkeypatch):
    """Frames are moved to the share as they are written, so the ram disk holds only the
    backlog -- until the share stalls. Then the run must stop while frames are still whole,
    rather than fill the disk and fail part-way through writing one."""
    import flux_metering.session as session_module

    s, _unit, _mount, _meter = session(peak_cell=(1, 0))
    monkeypatch.setattr(session_module.filer, "flush", lambda **kw: False)
    monkeypatch.setattr(s, "_ram_disk_free_bytes", lambda: 100 * 1024**2)  # 100 MB left

    assert s._walk_spiral() == "disk_full"
    assert "not draining" in (s.state.last_error or "")


def test_a_transient_backlog_only_pauses_the_run(session, monkeypatch):
    """A share hiccup that the mover recovers from must not end a 40-minute run."""
    import flux_metering.session as session_module

    s, _unit, _mount, _meter = session(peak_cell=(1, 0))
    readings = iter([100 * 1024**2])  # low once, then plenty

    def free_space():
        return next(readings, 50 * 1024**3)

    monkeypatch.setattr(session_module.filer, "flush", lambda **kw: True)
    monkeypatch.setattr(s, "_ram_disk_free_bytes", free_space)

    assert s._walk_spiral() == "converged"


def test_unreadable_free_space_does_not_stop_a_run(session, monkeypatch):
    """A run must be stopped by a real answer, never by the inability to ask."""
    s, _unit, _mount, _meter = session(peak_cell=(1, 0))
    monkeypatch.setattr(s, "_ram_disk_free_bytes", lambda: None)

    assert s._walk_spiral() == "converged"


def test_step_frames_are_not_read_back(session, monkeypatch):
    """A step keeps the file name and reads only the one frame that turns out to matter, so
    nothing pays for a 94 MB read and a 374 MB float64 array per exposure."""
    s, _unit, _mount, _meter = session(peak_cell=(1, 0))
    reads: list[str] = []
    original = s._expose_imager

    def counting(file_name, read_back=False):
        reads.append(f"{file_name}:{read_back}")
        return original(file_name, read_back)

    monkeypatch.setattr(s, "_expose_imager", counting)
    s._walk_spiral()

    assert reads, "the walk must have exposed something"
    assert all(entry.endswith(":False") for entry in reads), reads


def test_the_coordinates_the_operator_actually_types_are_accepted():
    """`RA_PATTERN` accepts space-separated sexagesimal, so `float()` is not enough --
    which is how a real request with `03 08 10.142` blew up. `acquirer.py` carries a
    comment about having fixed this exact defect once already."""
    from flux_metering.session import parse_target

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
    from flux_metering.session import parse_target

    parsed_ra, parsed_dec = parse_target(ra, dec)

    assert parsed_ra == pytest.approx(12.5125, abs=1e-4)
    assert parsed_dec == pytest.approx(-45.5, abs=1e-4)


def test_absent_coordinates_stay_absent():
    """None means 'take it from the mount', which only the acquirer can do."""
    from flux_metering.session import parse_target

    assert parse_target(None, None) == (None, None)


def test_an_ra_of_zero_is_a_coordinate_not_a_missing_value():
    """Truthiness would send RA 0h to the mount instead of using it."""
    from flux_metering.session import parse_target

    assert parse_target(0.0, 0.0) == (0.0, 0.0)


def test_a_bad_coordinate_raises_rather_than_being_guessed_at():
    from flux_metering.session import parse_target

    with pytest.raises(ValueError):
        parse_target("not a coordinate", None)


def test_a_moved_frame_is_read_from_the_share(tmp_path, monkeypatch):
    """The fallback is the normal path, not an edge case: frames go to the mover as they
    are written, so the arg-max frame has usually LEFT the ram disk by the time the
    correlation wants it. A first real run failed here because `change_top_to` compares
    against roots spelled POSIX-style while the folder came from pathlib with backslashes,
    so the prefix test silently missed."""
    import flux_metering.session as session_module
    from common.filer import FilerTop

    ram, shared = tmp_path / "ram" / "run", tmp_path / "shared" / "run"
    shared.mkdir(parents=True)
    ram.mkdir(parents=True)
    fits.PrimaryHDU(data=np.full((8, 8), 7, dtype=np.uint16)).writeto(shared / "step-00000-00.fits")

    def change_top_to(top, path):
        # As the real one does: a plain prefix test against a posix-spelled root.
        assert top is FilerTop.Shared
        posix_ram = ram.as_posix()
        return path.replace(posix_ram, shared.as_posix()) if path.startswith(posix_ram) else None

    monkeypatch.setattr(session_module.filer, "change_top_to", change_top_to)

    s = FluxMeteringSession.__new__(FluxMeteringSession)
    s.state = session_module.FluxMeteringStatus(folder=str(ram))  # backslashes on Windows

    data = s._read_fits("step-00000-00.fits")

    assert data.shape == (8, 8)
    assert data[0][0] == 7


def test_a_frame_that_is_nowhere_says_where_it_looked(tmp_path, monkeypatch):
    import flux_metering.session as session_module

    monkeypatch.setattr(session_module.filer, "change_top_to", lambda top, path: None)
    s = FluxMeteringSession.__new__(FluxMeteringSession)
    s.state = session_module.FluxMeteringStatus(folder=str(tmp_path))

    with pytest.raises(session_module.FluxMeteringError, match="neither on the ram disk"):
        s._read_fits("missing.fits")


# --------------------------------------------------------------- frame headers --
#
# The ThorCam frames used to carry only the six mandatory structural keywords, so one
# separated from its run folder was anonymous -- and `flux-00007-00.fits` is a name that
# repeats in every run on the share. These pin what each frame now says about itself.


@pytest.fixture
def walked(session, tmp_path):
    """A finished walk whose products sit in a properly shaped run folder.

    The folder shape matters: RUNDATE and RUNSEQ are read back out of it, so a bare
    tmp_path would omit them and the test would prove nothing.
    """
    folder = tmp_path / "2026-09-01" / "FluxMetering" / "0004"
    folder.mkdir(parents=True)

    def build(**params):
        s, unit, mount, meter = session(**params)
        s.state.folder = str(folder)
        s._expose_reference()
        s._walk_spiral()
        return s, folder

    return build


def _header(folder, name):
    return fits.getheader(str(folder / name))


def test_a_flux_frame_carries_the_settings_it_was_taken_with(walked):
    _s, folder = walked(flux_gain=7, flux_black_level=3)
    h = _header(folder, "flux-00000-00.fits")

    assert h["INSTRUME"] == "SimulatedFluxMeter"  # the CAMERA, not the hostname
    assert h["TELESCOP"] == "test-unit"  # the hostname belongs here
    assert h["CAMSN"] == "simulated"
    assert h["GAIN"] == 7
    assert h["CREATOR"] == "MAST flux_metering"
    assert h["DATE-OBS"] and h["DATE-END"]
    assert "EXPOSURE" not in h  # EXPTIME only; not a third convention


def test_the_black_level_is_recorded_so_the_frame_can_be_re_reduced(walked):
    """`frame_flux` subtracts it, so a frame without it cannot have its flux recomputed --
    the number needed for the sum absent from the data being summed."""
    _s, folder = walked(flux_black_level=5)
    assert _header(folder, "flux-00000-00.fits")["BLKLEVEL"] == 5


def test_a_step_frame_locates_itself_in_its_run(walked):
    _s, folder = walked()
    h = _header(folder, "flux-00000-00.fits")

    assert h["RUNDATE"] == "2026-09-01"
    assert h["RUNSEQ"] == "0004"
    assert h["STEPIDX"] == 0
    assert h["RING"] == 0
    # The one card tying this frame to the imager frame of the same exposure window; that
    # pairing otherwise exists only inside result.json.
    assert h["IMGFRAME"] == "step-00000-00.fits"


def test_the_cards_describe_the_step_being_exposed_not_the_previous_one(walked):
    """`self.state` is advanced only AFTER a step's exposures finish, so cards read off it
    would be one step stale. They come from `_measure_step`'s arguments instead."""
    s, folder = walked()
    later = next(step for step in s.steps if step.index == 3)
    h = _header(folder, "flux-00003-00.fits")

    assert h["STEPIDX"] == 3
    assert (h["CELLX"], h["CELLY"]) == tuple(later.cell)
    assert h["RING"] == later.ring


def test_the_reference_frame_omits_the_step_cards(walked):
    """Omitted, not zeroed: zeros would read as the origin cell, which is a real cell."""
    _s, folder = walked()
    h = _header(folder, "reference-flux-00.fits")

    for card in ("STEPIDX", "CELLX", "CELLY", "RING", "OFFRA", "OFFDEC"):
        assert card not in h
    # It is still identifiable as a frame of this run.
    assert h["CREATOR"] == "MAST flux_metering"
    assert h["RUNSEQ"] == "0004"
    assert h["IMGFRAME"] == "reference-00.fits"


def test_the_pointing_is_recorded_in_degrees_and_named(walked):
    _s, folder = walked()
    h = _header(folder, "flux-00000-00.fits")

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
    h = _header(folder, "flux-00000-00.fits")

    for card in ("CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2", "CTYPE1", "CTYPE2", "CD1_1", "CDELT1"):
        assert card not in h


def test_an_unrequested_target_is_absent_rather_than_defaulted(walked):
    """On a skip_acquisition run no target was asked for. Absent, so a reader can tell that
    from a target that happened to be at the origin."""
    _s, folder = walked()
    assert "OBJECT" not in _header(folder, "flux-00000-00.fits")


def test_a_requested_target_is_recorded(walked):
    _s, folder = walked(ra_j2000_hours=13.5, dec_j2000_degs=41.0)
    assert "13.5" in _header(folder, "flux-00000-00.fits")["OBJECT"]


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
    import flux_metering.session as session_module

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


# ------------------------------------------------- solving the reference frame --


def _fake_solver(monkeypatch, *, result=None, raises=None, delay=0.0, recorder=None):
    """Stand in for MastrometryDotNet. Imported inside `do_solve_reference`, so the patch
    goes on the module it is imported FROM."""
    import solvers.mastrometry as mastrometry_module

    class FakeSolver:
        def solve(self, unit=None, phase=None, full_frame_input_image_path=None, **kw):
            if recorder is not None:
                recorder.append(full_frame_input_image_path)
            if delay:
                time.sleep(delay)
            if raises is not None:
                raise raises
            return result

    monkeypatch.setattr(mastrometry_module, "MastrometryDotNet", FakeSolver)


def _solution(**kw):
    from common.solving import SolvingResult, SolvingSolution

    base = {"ra_hours": 3.967, "dec_degs": -13.51, "pixel_scale": 0.524085, "rotation_angle_degs": 158.559}
    return SolvingResult(succeeded=True, solution=SolvingSolution(**{**base, **kw}))


def _ready(session, tmp_path, name="reference-00.fits"):
    s, unit, _mount, _meter = session()
    (tmp_path / name).write_bytes(b"not really a fits, the solver is faked")
    s.state.reference_frame = name
    return s, unit


def test_a_solved_reference_is_recorded(session, tmp_path, monkeypatch):
    _fake_solver(monkeypatch, result=_solution())
    s, _unit = _ready(session, tmp_path)

    s.start_solving_the_reference()
    s._solve_thread.join(5)

    assert s._reference_solution["succeeded"] is True
    assert s._reference_solution["pixel_scale"] == pytest.approx(0.524085)
    assert s._reference_solution["rotation_angle_degs"] == pytest.approx(158.559)
    assert s._reference_solution["frame"] == "reference-00.fits"


def test_the_solve_does_not_block_the_walk(session, tmp_path, monkeypatch):
    """It runs while the spiral walks. A solve takes ~13 s against a spiral of tens of
    minutes, and nothing in the walk depends on the answer."""
    _fake_solver(monkeypatch, result=_solution(), delay=1.0)
    s, _unit = _ready(session, tmp_path)

    started = time.monotonic()
    s.start_solving_the_reference()
    returned_in = time.monotonic() - started

    assert returned_in < 0.5, "start_solving_the_reference blocked"
    assert s._solve_thread.is_alive()
    s._solve_thread.join(5)


def test_a_solver_that_raises_does_not_fail_the_run(session, tmp_path, monkeypatch):
    """A WCS is worth having and is not worth losing a spiral for."""
    _fake_solver(monkeypatch, raises=RuntimeError("solve-field is not installed"))
    s, _unit = _ready(session, tmp_path)

    s.start_solving_the_reference()
    s._solve_thread.join(5)

    assert s._reference_solution["succeeded"] is False
    assert "solve-field" in s._reference_solution["errors"][0]


def test_a_refusal_to_solve_is_recorded_rather_than_raised(session, tmp_path, monkeypatch):
    from common.solving import SolvingResult

    _fake_solver(monkeypatch, result=SolvingResult(succeeded=False, errors=["too few sources"]))
    s, _unit = _ready(session, tmp_path)

    s.start_solving_the_reference()
    s._solve_thread.join(5)

    assert s._reference_solution["succeeded"] is False
    assert s._reference_solution["errors"] == ["too few sources"]


def test_the_solver_is_handed_the_path_and_the_frame_is_left_alone(session, tmp_path, monkeypatch):
    """The frame is an input, never an output. The backend opens it read-only and writes
    its artifacts as `<frame>,solver=<name>.fits` beside it, so the original cannot be
    overwritten -- and a full frame is 94 MB, so it is passed by path, not read in."""
    seen: list = []
    _fake_solver(monkeypatch, result=_solution(), recorder=seen)
    s, _unit = _ready(session, tmp_path)
    frame = tmp_path / "reference-00.fits"
    before = frame.read_bytes()

    s.start_solving_the_reference()
    s._solve_thread.join(5)

    assert seen == [str(frame)]
    assert frame.read_bytes() == before


def test_nothing_is_solved_when_there_is_no_reference(session, tmp_path, monkeypatch):
    _fake_solver(monkeypatch, result=_solution())
    s, _unit, _mount, _meter = session()
    s.state.reference_frame = None

    s.start_solving_the_reference()

    assert s._solve_thread is None
    assert s._reference_solution is None
