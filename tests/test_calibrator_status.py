"""``/calibrate/status`` must always serialize.

The first tests in this repo, and deliberately so: they need no hardware and no
config DB, because they exercise a pure projection.

The regression they pin: ``latest["stage"]`` is a ``StageGeometryResult`` -- a
dataclass whose ``stage_positions`` / ``distances`` are ``np.ndarray``.  Pydantic
has no serializer for those and *raises* rather than skipping, so once a stage
calibration had run, every ``GET /calibrate/status`` returned 500.  Status is the
only channel through which a background run's outcome is observable, and
``latest[phase]`` is assigned *before* the ``has_solution`` check -- so a failed
run broke status too, exactly when the errors it carries are what you need.

Both serializers are asserted, because they are different code paths and only
the second is what actually runs in the server: ``model_dump_json`` (pydantic
direct) and ``jsonable_encoder`` (what FastAPI applies to a returned model).
"""

import json

import numpy as np
import pytest
from fastapi.encoders import jsonable_encoder

from calibration.analysis.models import HFDAutofocusResult, HFDAutofocusStatus, HFDFocusSample
from calibration.analysis.stage_geometry import StageGeometryResult
from calibration.calibrator import Calibrator, _jsonable
from common.canonical import CanonicalResponse
from common.extended_basemodel import ExtendedBaseModel


@pytest.fixture
def calibrator():
    """An unbound ``Calibrator`` -- no unit, so no hardware and no DB.

    ``Calibrator`` is a singleton whose ``__new__`` caches ``_instance``, so it
    is built the long way round and ``_initialized`` forced back to False;
    otherwise the second test in a session would silently reuse the first's
    ``latest`` dict and could pass on stale state.
    """
    c = Calibrator.__new__(Calibrator)
    c._initialized = False
    c.__init__(None)
    return c


def stage_result(**kwargs) -> StageGeometryResult:
    """A stage result with plausible values; override any field via kwargs."""
    return StageGeometryResult(
        **{
            "has_solution": True,
            "spec_position": 1234.0,
            "slope": 0.5,
            "intercept": -2.0,
            "n_frames": 5,
            "residual_rms": 1.1,
            "angle_rms_deg": 0.8,
            "bracketed": True,
            "optical_center": (2000.0, 1500.0),
            "centerline_angle": 0.01,
            "message": "ok",
            **kwargs,
        }
    )


def serialized(calibrator) -> str:
    """``status()`` as it goes on the wire, via the response model."""
    return CanonicalResponse(value=calibrator.status()).model_dump_json()


# --------------------------------------------------------------- the regression
@pytest.mark.parametrize(
    "latest_stage",
    [
        pytest.param(
            stage_result(stage_positions=np.arange(5.0), distances=np.linspace(-5, 5, 5)),
            id="arrays-populated",
        ),
        pytest.param(stage_result(), id="arrays-none-the-dataclass-default"),
        # The nastiest shape: a failed run assigns None, and status carries the
        # errors explaining why.  That must be readable above all.
        pytest.param(None, id="failed-run-none"),
    ],
)
def test_status_serializes_with_a_stage_result(calibrator, latest_stage):
    calibrator.latest["stage"] = latest_stage

    assert json.loads(serialized(calibrator))["value"]["latest"]
    jsonable_encoder(CanonicalResponse(value=calibrator.status()))


def test_status_serializes_when_empty(calibrator):
    """The idle case -- nothing has run yet."""
    assert json.loads(serialized(calibrator))["value"]["latest"] == {}


# ------------------------------------------------- what the projection preserves
def test_arrays_survive_as_lists(calibrator):
    """Converted, not dropped: they are ~5 points, so keeping them is free."""
    calibrator.latest["stage"] = stage_result(
        stage_positions=np.arange(5.0), distances=np.linspace(-5, 5, 5)
    )

    stage = json.loads(serialized(calibrator))["value"]["latest"]["stage"]

    assert stage["stage_positions"] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert stage["distances"] == [-5.0, -2.5, 0.0, 2.5, 5.0]
    assert stage["spec_position"] == 1234.0
    assert stage["bracketed"] is True


def test_latest_keeps_the_live_objects(calibrator):
    """The projection is for the wire only.

    ``self.latest`` stays the real objects so in-process inspection and plotting
    still work -- serializing must not quietly consume the run's own results.
    """
    result = stage_result(stage_positions=np.arange(3.0))
    calibrator.latest["stage"] = result

    serialized(calibrator)

    assert calibrator.latest["stage"] is result
    assert isinstance(calibrator.latest["stage"].stage_positions, np.ndarray)


def test_nan_survives_to_the_wire(calibrator):
    """A NaN sample is data, not absence.

    It is how a frame with no usable star is recorded, so it must reach the
    replay bundles: dropping it would silently shorten the sweep.

    On the wire it is the *string* ``"NaN"``, not a bare ``NaN`` token --
    ``ExtendedBaseModel``'s convention, which keeps the payload valid JSON
    (a bare NaN is not) and round-trips through ``custom_json_decoder``.  The
    projection preserves it only because it dumps models with
    ``mode="json"``; hand-rolling the dict would drop back to a raw float.
    """
    calibrator.latest["focuser"] = HFDAutofocusStatus(
        message="fit ok",
        errors=[],
        analysis_result=HFDAutofocusResult(
            has_solution=True,
            best_focus_position=24010.0,
            n_consistent_stars=41,
            focus_samples=[
                HFDFocusSample(
                    is_valid=False, focus_position=23900.0, num_stars=0, hfd_pixels=float("nan")
                )
            ],
        ),
    )

    payload = serialized(calibrator)
    sample = json.loads(payload)["value"]["latest"]["focuser"]["analysis_result"]["focus_samples"][0]

    assert sample["is_valid"] is False
    assert sample["hfd_pixels"] == "NaN"
    assert np.isnan(ExtendedBaseModel.custom_json_decoder(sample["hfd_pixels"]))

    # No bare NaN/Infinity token anywhere: those parse in Python but are invalid
    # JSON, and the GUI consuming this is not Python.
    def reject(token):
        raise AssertionError(f"bare {token} token in the payload")

    json.loads(payload, parse_constant=reject)


def test_numpy_scalars_become_python_scalars():
    """``np.float64`` / ``np.int64`` leak out of any numpy arithmetic."""
    out = _jsonable({"a": np.float64(3.5), "b": np.int64(7)})

    assert out == {"a": 3.5, "b": 7}
    assert [type(v) for v in out.values()] == [float, int]


def test_nesting_is_projected_all_the_way_down():
    """Arrays are reached inside dicts and lists, not just at the top level."""
    out = _jsonable({"runs": [{"d": np.array([1.0, 2.0])}, (np.int64(3),)]})

    assert out == {"runs": [{"d": [1.0, 2.0]}, [3]]}


# ------------------------------------------------------- the rest of the payload
def test_status_shape_is_unchanged(calibrator):
    """The projection touches ``latest`` only."""
    status = calibrator.status()

    assert set(status) == {"calibrating", "umbrella", "phase", "products", "latest", "errors"}
    assert status["calibrating"] is False
    assert status["phase"] is None
    assert status["products"] == {"focuser": False, "optical_center": False, "stage": False}


# --------------------------------------------------------- run-start clearing
def test_start_clears_latest_from_the_previous_run(calibrator):
    """A new run must not serve the previous run's result as if it were current.

    ``latest`` used to survive across runs while ``errors`` was cleared, so
    ``/calibrate/status`` polled during run N showed run N-1's result -- which
    reads exactly like a finished, failed run and is indistinguishable from one.
    Status is the only channel a background run has.
    """
    from unittest.mock import MagicMock

    calibrator.unit = MagicMock()
    calibrator.unit.is_active.return_value = False  # nothing running -> _start proceeds
    calibrator.latest["focuser"] = {"stale": "from the previous run"}
    calibrator.errors = ["stale error"]

    calibrator._start(lambda: None)

    assert calibrator.latest == {}
    assert calibrator.errors == []
