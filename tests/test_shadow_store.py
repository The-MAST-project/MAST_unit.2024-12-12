"""The shadow models kept locally (`imaging/shadow_store.py`): what is saved is the whole model, a lookup asks the
*stage* whether a stored position is its own, and nothing the store does can fail a solve."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits

from acquisition import Acquisition
from imaging.mirror_shadow import ShadowModel
from imaging.shadow_store import ShadowStore, StoredShadow, frame_binning, frame_shape, get_shadow, within
from solvers.source_selection import _shadow_deficit

SHAPE = (100, 200)
TOLERANCE = 10


class FakeStage:
    """The part of `Stage` the store uses: `position`, and `close_enough(target)` against its own tolerance."""

    def __init__(self, position: int | None = 41000, tolerance: int = TOLERANCE):
        self.position = position
        self.tolerance = tolerance
        self.asked: list[int] = []
        self.conf = SimpleNamespace(close_enough=tolerance)

    def close_enough(self, target) -> bool:
        self.asked.append(target)
        return self.position is not None and abs(self.position - target) <= self.tolerance


def _model(offset=12.0, shape=SHAPE, present=True) -> ShadowModel:
    s = np.arange(-60.0, 61.0, 6.0)
    profile = np.clip(0.5 * (30.0 - np.abs(s - offset)) / 20.0, 0.0, 0.5)
    return ShadowModel(
        present=present,
        image_shape=shape,
        angle=0.05,
        offset=offset,
        umbra_half_width=10.0,
        penumbra_half_width=30.0,
        depth=0.5,
        prominence=20.0,
        baseline=0.01,
        two_sided_depth=0.4,
        umbra_s=(offset - 10.0, np.inf),  # a side that runs off the frame
        penumbra_s=(-np.inf, offset + 30.0),
        s=s,
        profile=profile,
    )


def _entry(position=41000, offset=12.0, shape=SHAPE, detected_at=None, phase="spec") -> StoredShadow:
    return StoredShadow(
        model=_model(offset, shape),
        stage_position=position,
        detected_at=detected_at or datetime.datetime.now(datetime.UTC).isoformat(),
        seconds=7.5,
        image_path="a.fits",
        phase=phase,
    )


@pytest.fixture
def store(tmp_path) -> ShadowStore:
    return ShadowStore(tmp_path / "mast99")


def _native(store: ShadowStore) -> list[StoredShadow]:
    """What was detected: a binning 1 entry also leaves a derived binning 2 one, which these tests are not about."""
    return [e for e in store.entries() if not e.derived]


# ------------------------------------------------------------------------------------------------ the model


def test_a_model_survives_json_whole():
    original = _model()

    again = ShadowModel.from_dict(json.loads(json.dumps(original.to_dict())))  # strict JSON: no Infinity

    assert again.present and tuple(again.image_shape) == SHAPE
    assert (again.angle, again.offset, again.depth, again.baseline) == (0.05, 12.0, 0.5, 0.01)
    assert again.umbra_s == (2.0, np.inf) and again.penumbra_s == (-np.inf, 42.0)  # open sides stay open
    assert np.array_equal(again.s, original.s) and np.array_equal(again.profile, original.profile)


def test_a_stored_model_selects_as_the_detected_one_did():
    """The selection reads each source's deficit off the measured cross-section: it must be kept, or a stored model would
    quietly fall back to a trapezoid."""
    original = _model()
    again = ShadowModel.from_dict(json.loads(json.dumps(original.to_dict())))
    x, y = np.array([20.0, 100.0, 150.0, 190.0]), np.array([10.0, 50.0, 60.0, 90.0])

    assert np.array_equal(_shadow_deficit(original, x, y, SHAPE)[0], _shadow_deficit(again, x, y, SHAPE)[0])
    assert _shadow_deficit(again, x, y, SHAPE)[0].max() > 0.1


def test_a_model_without_a_cross_section_round_trips_too():
    bare = ShadowModel(present=True, image_shape=SHAPE, offset=5.0, umbra_half_width=4.0, penumbra_half_width=9.0)

    again = ShadowModel.from_dict(bare.to_dict())

    assert again.s is None and again.profile is None and again.umbra_s is None


# ------------------------------------------------------------------------------------------------ the store


def test_it_lives_in_the_calibration_folder_beside_the_hot_pixels(tmp_path):
    store = ShadowStore(tmp_path / "mast99")
    store.save(_entry(), within(41000, TOLERANCE))

    assert store.path == tmp_path / "mast99" / "Calibration" / "shadow_models.json" and store.path.exists()


def test_a_saved_model_is_found_where_the_stage_says_it_is(store):
    store.save(_entry(), within(41000, TOLERANCE))
    stage = FakeStage(41000 + TOLERANCE)

    found = store.find(stage.position, stage.close_enough, SHAPE)

    assert found is not None and found.stage_position == 41000 and found.model.offset == 12.0
    assert 41000 in stage.asked  # the stage was asked whether the stored position is its own


def test_a_position_the_stage_does_not_call_close_is_not_found(store):
    store.save(_entry(), within(41000, TOLERANCE))
    stage = FakeStage(41000 + TOLERANCE + 1)

    assert store.find(stage.position, stage.close_enough, SHAPE) is None


def test_what_counts_as_close_is_the_stages_to_say(store):
    store.save(_entry(position=1000), within(1000, TOLERANCE))

    assert store.find(50000, lambda stored: True, SHAPE) is not None  # a stage that says so is believed
    assert store.find(1000, lambda stored: False, SHAPE) is None  # and one that says not, even at the same number


def test_the_nearest_of_the_close_entries_wins(store):
    store.save(_entry(position=41000, offset=1.0), within(41000, 3))
    store.save(_entry(position=41008, offset=2.0), within(41008, 3))
    stage = FakeStage(41006, tolerance=20)

    assert store.find(stage.position, stage.close_enough, SHAPE).model.offset == 2.0  # type: ignore[union-attr]


def test_another_frame_shape_has_its_own_entry(store):
    store.save(_entry(), within(41000, TOLERANCE))

    assert store.find(41000, within(41000, TOLERANCE), (60, 120)) is None  # (50, 100) is the derived binning 2 one


def test_without_a_position_or_a_shape_there_is_nothing_to_key_on(store):
    store.save(_entry(), within(41000, TOLERANCE))

    assert store.find(None, lambda p: True, SHAPE) is None
    assert store.find(41000, within(41000, TOLERANCE), None) is None


def test_saving_replaces_what_the_stage_calls_the_same_position_and_keeps_the_rest(store):
    store.save(_entry(position=41000, offset=1.0), within(41000, TOLERANCE))
    store.save(_entry(position=60000, offset=9.0), within(60000, TOLERANCE))

    store.save(_entry(position=41004, offset=2.0), within(41004, TOLERANCE))

    assert sorted((e.stage_position, e.model.offset) for e in _native(store)) == [(41004, 2.0), (60000, 9.0)]


def test_a_new_store_object_reads_what_an_earlier_one_wrote(tmp_path):
    ShadowStore(tmp_path / "mast99").save(_entry(), within(41000, TOLERANCE))  # a process that has since ended

    found = ShadowStore(tmp_path / "mast99").find(41000, within(41000, TOLERANCE), SHAPE)

    assert found is not None and found.phase == "spec" and found.seconds == 7.5 and found.image_path == "a.fits"


def test_an_old_entry_can_be_refused(store):
    old = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=40)).isoformat()
    store.save(_entry(detected_at=old), within(41000, TOLERANCE))
    is_close = within(41000, TOLERANCE)

    assert store.find(41000, is_close, SHAPE) is not None
    assert store.find(41000, is_close, SHAPE, max_age_days=30) is None
    assert store.find(41000, is_close, SHAPE, max_age_days=60) is not None


def test_remove_drops_what_the_stage_calls_close(store):
    store.save(_entry(position=41000), within(41000, TOLERANCE))
    store.save(_entry(position=60000), within(60000, TOLERANCE))

    assert store.remove(within(41003, TOLERANCE)) == 2  # the model and its derived binning 2 one
    assert [e.stage_position for e in _native(store)] == [60000]


# ------------------------------------------------------------------------------------------------ it never raises


def test_a_damaged_file_is_an_empty_store(store, caplog):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")

    with caplog.at_level("WARNING"):
        assert store.entries() == [] and store.find(41000, within(41000, TOLERANCE), SHAPE) is None

    assert "unreadable" in caplog.text
    assert store.save(_entry(), within(41000, TOLERANCE)) is True  # and is simply written over
    assert len(_native(store)) == 1


def test_another_version_is_ignored(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"version": 99, "entries": [_entry().to_dict()]}))

    assert store.entries() == []


def test_one_bad_entry_does_not_lose_the_others(store):
    good = _entry(position=60000).to_dict()
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"version": 1, "entries": [{"stage_position": 1}, good]}))

    assert [e.stage_position for e in store.entries()] == [60000]


def test_a_store_that_cannot_be_written_says_so(tmp_path, caplog):
    blocker = tmp_path / "mast99"
    blocker.write_text("a file where the unit's folder should be")

    with caplog.at_level("WARNING"):
        assert ShadowStore(blocker).save(_entry(), within(41000, TOLERANCE)) is False

    assert "could not write" in caplog.text


def test_the_file_is_replaced_whole_never_left_half_written(store):
    store.save(_entry(), within(41000, TOLERANCE))

    assert not store.path.with_name(store.path.name + ".tmp").exists()
    assert json.loads(store.path.read_text())["version"] == 1


def test_frame_shape_reads_an_array_or_a_fits_header(tmp_path):
    path = tmp_path / "f.fits"
    fits.writeto(path, np.zeros(SHAPE, dtype=np.uint16))

    assert frame_shape(np.zeros(SHAPE)) == SHAPE and frame_shape(path) == SHAPE
    assert frame_shape(tmp_path / "missing.fits") is None


# ---------------------------------------------------------------------- get_shadow, with no acquisition


class Detector:
    def __init__(self, present=True, fail=False):
        self.calls = 0
        self.present, self.fail = present, fail

    def __call__(self, frame):
        self.calls += 1
        if self.fail:
            raise RuntimeError("no")
        return _model(present=self.present)


@pytest.fixture
def frame(tmp_path):
    path = tmp_path / "frame.fits"
    fits.writeto(path, np.zeros(SHAPE, dtype=np.uint16))
    return path


def test_a_stored_model_is_used_without_detecting(store, frame):
    store.save(_entry(), within(41000, TOLERANCE))
    stage, detector = FakeStage(), Detector()

    result = get_shadow(frame, stage.position, stage.close_enough, store=store, detector=detector)

    assert result.source == "stored" and result.model is not None and detector.calls == 0


def test_with_none_stored_it_detects_and_keeps_it_for_the_next_caller(store, frame):
    stage, detector = FakeStage(), Detector()

    first = get_shadow(frame, stage.position, stage.close_enough, store=store, phase="spec", detector=detector)
    second = get_shadow(frame, stage.position, stage.close_enough, store=store, detector=detector)

    assert (first.source, second.source, detector.calls) == ("detected", "stored", 1)
    assert store.entries()[0].phase == "spec" and store.entries()[0].image_path == str(frame)


def test_refresh_detects_again_and_replaces(store, frame):
    store.save(_entry(offset=99.0), within(41000, TOLERANCE))
    stage, detector = FakeStage(), Detector()

    result = get_shadow(frame, stage.position, stage.close_enough, store=store, detector=detector, refresh=True)

    assert result.source == "detected" and detector.calls == 1
    assert [e.model.offset for e in _native(store)] == [12.0]


def test_a_stage_that_cannot_say_where_it_is_gets_a_detection_and_nothing_is_kept(store, frame):
    stage, detector = FakeStage(position=None), Detector()

    result = get_shadow(frame, stage.position, stage.close_enough, store=store, detector=detector)

    assert result.source == "detected" and store.entries() == []


def test_no_band_is_not_kept_on_disk(store, frame):
    """One frame through cloud must not stand for a position until someone notices."""
    stage, detector = FakeStage(), Detector(present=False)

    result = get_shadow(frame, stage.position, stage.close_enough, store=store, detector=detector)

    assert result.model is not None and result.model.present is False and store.entries() == []


def test_a_detection_that_fails_is_not_an_exception(store, frame):
    stage = FakeStage()

    result = get_shadow(frame, stage.position, stage.close_enough, store=store, detector=Detector(fail=True))

    assert result.source == "failed" and result.model is None and store.entries() == []


def test_without_a_store_it_just_detects(frame):
    stage, detector = FakeStage(), Detector()

    result = get_shadow(frame, stage.position, stage.close_enough, store=None, detector=detector)

    assert result.source == "detected" and result.entry is None


# ------------------------------------------------------------------------------------------------ the acquisition


def _acq(store, stage, monkeypatch, detector):
    acq = Acquisition.__new__(Acquisition)
    acq.shadow = None
    acq.shadow_store = store
    acq.unit = SimpleNamespace(stage=stage)
    monkeypatch.setattr("acquisition.detect_mirror_shadow", detector)
    return acq


def test_an_acquisition_finds_what_an_earlier_one_detected(store, frame, monkeypatch):
    stage, detector = FakeStage(), Detector()
    _acq(store, stage, monkeypatch, detector).shadow_for(str(frame), "spec")

    later = _acq(store, stage, monkeypatch, detector)  # another process, another acquisition: nothing in memory
    model = later.shadow_for(str(frame), "spec")

    assert detector.calls == 1 and model is not None and later.shadow is not None and later.shadow.seconds == 0.0


def test_the_acquisition_asks_the_stage_and_ignores_the_phase_on_disk(store, frame, monkeypatch):
    stage, detector = FakeStage(), Detector()
    first = _acq(store, stage, monkeypatch, detector)
    first.shadow_for(str(frame), "spec")
    stage.asked.clear()

    first.shadow_for(str(frame), "guiding")  # the memory cache is per phase; the disk is per stage position

    assert detector.calls == 1 and stage.asked == [41000]


def test_the_acquisition_detects_again_when_the_stage_has_moved(store, frame, monkeypatch):
    stage, detector = FakeStage(), Detector()
    acq = _acq(store, stage, monkeypatch, detector)
    acq.shadow_for(str(frame), "spec")

    stage.position = 41000 + TOLERANCE + 1
    acq.shadow_for(str(frame), "spec")

    assert detector.calls == 2 and len(_native(store)) == 2


def test_a_stage_without_a_close_enough_method_falls_back_to_its_configuration(store, frame, monkeypatch):
    # as the older tests build a stage: a position and a configuration, no method
    stage = SimpleNamespace(position=41000, conf=SimpleNamespace(close_enough=TOLERANCE))
    detector = Detector()
    _acq(store, stage, monkeypatch, detector).shadow_for(str(frame), "spec")

    stage.position = 41000 + TOLERANCE
    _acq(store, stage, monkeypatch, detector).shadow_for(str(frame), "spec")

    assert detector.calls == 1


def test_an_acquisition_with_no_store_is_as_it_was(frame, monkeypatch):
    stage, detector = FakeStage(), Detector()
    acq = _acq(None, stage, monkeypatch, detector)

    acq.shadow_for(str(frame), "spec")
    acq.shadow_for(str(frame), "sky")  # another phase, in memory only: detected again

    assert detector.calls == 2


def test_the_stores_path_follows_the_unit_folder(tmp_path):
    assert ShadowStore(Path(tmp_path) / "u").path.parts[-2:] == ("Calibration", "shadow_models.json")


# ------------------------------------------------------------------------------------------------ binnings 1 and 2

FULL = (100, 200)  # the pretend full sensor: binning 1
BINNED = (50, 100)  # the same frame, 2x2


def _full_entry(position=41000, offset=12.0, binning=1) -> StoredShadow:
    shape = FULL if binning == 1 else BINNED
    entry = _entry(position, offset, shape)
    entry.binning = binning
    return entry


def test_the_machines_store_is_in_the_local_filing_area(monkeypatch, tmp_path):
    import common.filer as filer_module

    class FakeFiler:
        local = SimpleNamespace(root=str(tmp_path) + "/")
        shared = SimpleNamespace(root="//somewhere/else/")

    monkeypatch.setattr(filer_module, "Filer", FakeFiler)

    store = ShadowStore.for_machine()

    assert store is not None and store.path == tmp_path / "Calibration" / "shadow_models.json"


def test_a_binning_1_model_is_also_kept_for_binning_2_as_a_derived_one(store):
    assert store.save(_full_entry(), within(41000, TOLERANCE))

    two = store.find(41000, within(41000, TOLERANCE), BINNED)
    one = store.find(41000, within(41000, TOLERANCE), FULL)

    assert one is not None and one.binning == 1 and not one.derived
    assert two is not None and two.binning == 2 and two.derived
    assert tuple(two.model.image_shape) == BINNED and two.model.offset == one.model.offset / 2
    assert two.detected_at == one.detected_at and two.image_path == one.image_path


def test_a_derived_model_reads_the_deficit_the_full_one_does(store):
    store.save(_full_entry(), within(41000, TOLERANCE))
    one = store.find(41000, within(41000, TOLERANCE), FULL).model
    two = store.find(41000, within(41000, TOLERANCE), BINNED).model
    x, y = np.array([20.0, 100.0, 150.0, 190.0]), np.array([10.0, 50.0, 60.0, 90.0])

    full = _shadow_deficit(one, x, y, FULL)[0]
    binned = _shadow_deficit(two, (x - 0.5) / 2, (y - 0.5) / 2, BINNED)[0]

    assert np.allclose(full, binned, atol=0.03)


def test_a_new_binning_1_detection_replaces_the_derived_entry(store):
    store.save(_full_entry(offset=12.0), within(41000, TOLERANCE))

    store.save(_full_entry(offset=20.0), within(41000, TOLERANCE))

    two = store.find(41000, within(41000, TOLERANCE), BINNED)
    assert two is not None and two.derived and two.model.offset == 10.0
    assert len(store.entries()) == 2


def test_a_model_detected_at_binning_2_replaces_the_derived_one(store):
    store.save(_full_entry(offset=12.0), within(41000, TOLERANCE))

    store.save(_full_entry(offset=9.0, binning=2), within(41000, TOLERANCE))

    two = store.find(41000, within(41000, TOLERANCE), BINNED)
    assert two is not None and not two.derived and two.binning == 2 and two.model.offset == 9.0
    assert len(store.entries()) == 2


def test_a_later_binning_1_detection_does_not_replace_a_native_binning_2_one(store):
    store.save(_full_entry(offset=9.0, binning=2), within(41000, TOLERANCE))

    store.save(_full_entry(offset=30.0), within(41000, TOLERANCE))

    two = store.find(41000, within(41000, TOLERANCE), BINNED)
    one = store.find(41000, within(41000, TOLERANCE), FULL)
    assert two is not None and not two.derived and two.model.offset == 9.0
    assert one is not None and one.model.offset == 30.0


def test_a_frame_whose_sides_are_odd_has_no_derived_entry(store):
    odd = _entry(41000, 12.0, (101, 199))

    store.save(odd, within(41000, TOLERANCE))

    assert [tuple(e.model.image_shape) for e in store.entries()] == [(101, 199)]


def test_a_file_written_before_binnings_were_recorded_reads_as_binning_1(store):
    entry = _entry()
    document = {"version": 1, "entries": [entry.to_dict()]}
    del document["entries"][0]["binning"], document["entries"][0]["derived"]
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(document), encoding="utf-8")

    (read,) = store.entries()

    assert read.binning == 1 and not read.derived


def test_the_binning_of_a_frame_is_its_header_s(tmp_path):
    path = tmp_path / "f.fits"
    header = fits.Header()
    header["XBINNING"] = 2
    fits.writeto(path, np.zeros((4, 4), dtype=np.uint16), header)

    assert frame_binning(path) == 2 and frame_binning(np.zeros((4, 4))) == 1 and frame_binning(tmp_path / "none.fits") == 1


def test_get_shadow_keeps_the_binning_it_was_given_and_the_derived_one(store):
    detected = _model(shape=FULL)

    result = get_shadow(np.zeros(FULL), 41000, within(41000, TOLERANCE), store=store, detector=lambda frame: detected)

    assert result.source == "detected" and result.entry is not None and result.entry.binning == 1
    assert store.find(41000, within(41000, TOLERANCE), BINNED) is not None
