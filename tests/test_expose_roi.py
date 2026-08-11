"""The ROI contract of the `expose` endpoint.

The four ROI parameters are all-or-nothing. A partial set used to be accepted and then
fail silently: `expose` passes them POSITIONALLY into a thread, so a None overrode
`do_expose`'s own default, `UnitRoi` accepted it (a plain class, no validation), and
`ImagerRoi.from_other` died on `None - int` inside the thread -- after the endpoint had
already returned CanonicalResponse_Ok. The caller saw success and got no image.

Tested through the resolver rather than the endpoint, which would need a live Unit
singleton and hardware. The resolver lives in its own import-free module for the same
reason: importing `unit` costs ~1400 modules and pulls in PlaneWave, PHD2 and the solver.
"""

from __future__ import annotations

import pytest

from exposure_roi import resolve_exposure_roi

SENSOR_X, SENSOR_Y = 8288, 5644


class TestNothingSupplied:
    def test_all_none_means_the_whole_sensor(self):
        assert resolve_exposure_roi(None, None, None, None, SENSOR_X, SENSOR_Y) == (
            SENSOR_X // 2,
            SENSOR_Y // 2,
            SENSOR_X,
            SENSOR_Y,
        )

    @pytest.mark.parametrize(("x", "y"), [(None, SENSOR_Y), (SENSOR_X, None), (0, 0)], ids=["no x", "no y", "zeroes"])
    def test_an_imager_that_cannot_state_its_size_is_an_error(self, x, y):
        with pytest.raises(ValueError, match="cannot get width and height"):
            resolve_exposure_roi(None, None, None, None, x, y)


class TestAllSupplied:
    def test_all_four_are_passed_through_untouched(self):
        assert resolve_exposure_roi(4335, 3095, 1500, 1300, SENSOR_X, SENSOR_Y) == (4335, 3095, 1500, 1300)

    def test_zero_is_a_value_not_an_omission(self):
        """0 is falsy but a perfectly good coordinate; only None means 'not supplied'."""
        assert resolve_exposure_roi(0, 0, 100, 100, SENSOR_X, SENSOR_Y) == (0, 0, 100, 100)


class TestPartiallySupplied:
    """The case that used to produce a confident 'ok' and no image."""

    @pytest.mark.parametrize(
        ("args", "expected_missing"),
        [
            ((None, None, 1000, None), ["fiber_x", "fiber_y", "height"]),
            ((4335, 3095, None, None), ["width", "height"]),
            ((None, 3095, 1500, 1300), ["fiber_x"]),
            ((4335, 3095, 1500, None), ["height"]),
        ],
        ids=["width only", "centre only", "no fiber_x", "no height"],
    )
    def test_any_missing_parameter_is_refused(self, args, expected_missing):
        with pytest.raises(ValueError, match="incomplete ROI") as excinfo:
            resolve_exposure_roi(*args, SENSOR_X, SENSOR_Y)

        message = str(excinfo.value)
        for name in expected_missing:
            assert name in message, f"the error must name '{name}' so the caller can fix it"

    def test_the_error_names_what_was_given_too(self):
        with pytest.raises(ValueError, match="given: width") as excinfo:
            resolve_exposure_roi(None, None, 1000, None, SENSOR_X, SENSOR_Y)
        assert "or none of them" in str(excinfo.value), "the error must say how to ask for the full frame"

    def test_a_partial_roi_never_reaches_the_imaging_layer(self):
        """The actual failure being prevented, spelled out: this is what used to happen
        to a partial set once it reached the imaging layer inside the thread."""
        from common.models.statuses import ImagerRoi
        from common.rois import UnitRoi

        with pytest.raises(TypeError):
            ImagerRoi.from_other(roi=UnitRoi(None, None, 1000, None))  # type: ignore[arg-type]
