"""The acquisition caches the shadow model per phase and stage position; the solver is handed it."""

from types import SimpleNamespace

import pytest

from acquisition import Acquisition
from imaging.mirror_shadow import ShadowModel, StagedShadow

CLOSE = 10


def _model(offset=12.0, shape=(100, 200)):
    return ShadowModel(
        present=True, image_shape=shape, angle=0.0, offset=offset, umbra_half_width=5.0, penumbra_half_width=9.0
    )


def _acq(position=41000):
    acq = Acquisition.__new__(Acquisition)
    acq.shadow = None
    acq.unit = SimpleNamespace(stage=SimpleNamespace(position=position, conf=SimpleNamespace(close_enough=CLOSE)))
    return acq


@pytest.fixture
def detector(monkeypatch):
    calls = []

    def fake(path, *a, **k):
        calls.append(path)
        return _model()

    monkeypatch.setattr("acquisition.detect_mirror_shadow", fake)
    return calls


def test_cached_while_phase_and_stage_hold(detector):
    acq = _acq()
    first = acq.shadow_for("a.fits", "spec")
    assert acq.shadow_for("b.fits", "spec") is first
    acq.unit.stage.position += CLOSE
    assert acq.shadow_for("c.fits", "spec") is first
    assert detector == ["a.fits"]


def test_redetected_when_stage_moves_or_phase_changes(detector):
    acq = _acq()
    acq.shadow_for("a.fits", "spec")
    acq.unit.stage.position += CLOSE + 1
    acq.shadow_for("b.fits", "spec")
    acq.shadow_for("c.fits", "sky")  # same stage, another phase: the band is not assumed to be where it was
    assert detector == ["a.fits", "b.fits", "c.fits"]
    assert isinstance(acq.shadow, StagedShadow) and acq.shadow.phase == "sky"


def test_failed_detection_is_not_cached(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr("acquisition.detect_mirror_shadow", boom)
    acq = _acq()
    assert acq.shadow_for("a.fits", "spec") is None and acq.shadow is None


def test_unknown_stage_position_matches_only_itself():
    staged = StagedShadow(_model(), None, "f", "t", 1.0, phase="spec")
    assert staged.matches(None, CLOSE, "spec") and not staged.matches(5, CLOSE, "spec")


def test_cropped_shifts_the_band_and_keeps_it():
    full = _model(offset=30.0, shape=(100, 200))
    crop = full.cropped(
        0, 20, 200, 40
    )  # angle 0: the band's coordinate is y from the centre; the crop's centre is 10 px higher
    assert crop.present and crop.image_shape == (40, 200)
    assert crop.offset == pytest.approx(40.0)
    assert not ShadowModel(present=False, image_shape=(1, 1)).cropped(0, 0, 1, 1).present
    assert crop.summary()["present"] is True
