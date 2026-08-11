"""The parameter contracts of the `expose` endpoint: ROI, and offsets.

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

from expose_params import MAX_OFFSET_ARCSEC, resolve_exposure_roi, resolve_offsets

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


class TestOffsets:
    """Offsets are arcseconds as plain decimals -- never sexagesimal -- so the only
    conversion is float(). It was previously unguarded, so bad text left the endpoint as
    a ValueError and reached the client as HTTP 500 instead of a CanonicalResponse.
    """

    def test_none_means_no_offsetting(self):
        assert resolve_offsets(None, 5, "ra_offsets") is None

    @pytest.mark.parametrize("empty", ["", "   ", []], ids=["empty string", "whitespace", "empty list"])
    def test_empty_also_means_no_offsetting(self, empty):
        """It used to report 'must have N elements', which describes the wrong problem."""
        assert resolve_offsets(empty, 5, "ra_offsets") is None

    def test_one_value_is_used_for_every_repeat(self):
        assert resolve_offsets("1.5", 3, "ra_offsets") == [1.5, 1.5, 1.5]

    def test_one_value_per_repeat_is_taken_as_given(self):
        assert resolve_offsets("1.5 -2 0", 3, "ra_offsets") == [1.5, -2.0, 0.0]

    def test_a_list_is_accepted_as_well_as_a_string(self):
        assert resolve_offsets([1.5, -2.0], 2, "dec_offsets") == [1.5, -2.0]

    def test_negative_and_decimal_arcseconds_are_fine(self):
        assert resolve_offsets("-0.25", 1, "ra_offsets") == [-0.25]

    def test_the_wrong_count_is_refused_and_says_both_forms(self):
        with pytest.raises(ValueError, match="has 2 values") as excinfo:
            resolve_offsets("1 2", 5, "ra_offsets")
        message = str(excinfo.value)
        assert "exactly one" in message and "exactly 5" in message, "both accepted forms must be stated"

    @pytest.mark.parametrize("bad", ["abc", "1 abc 3", "12:30:45"], ids=["text", "one bad of three", "sexagesimal"])
    def test_a_non_number_is_refused_rather_than_raising_out_of_the_endpoint(self, bad):
        repeats = 3 if " " in bad else 1
        with pytest.raises(ValueError, match="not a number") as excinfo:
            resolve_offsets(bad, repeats, "dec_offsets")
        assert "arcseconds" in str(excinfo.value), "the caller must be told what form is expected"
        assert "dec_offsets[" in str(excinfo.value), "and which value was bad"

    def test_every_bad_value_is_reported_not_just_the_first(self):
        """Fixing a list one rejection at a time is needless round trips when they are
        all visible in the same pass."""
        with pytest.raises(ValueError) as excinfo:
            resolve_offsets("1 abc 2.5 nan 1e9", 5, "ra_offsets")

        message = str(excinfo.value)
        assert "ra_offsets[1]='abc'" in message, "the non-number must be named"
        assert "ra_offsets[3]='nan'" in message, "the non-finite value must be named"
        assert "ra_offsets[4]" in message, "the out-of-range value must be named"
        assert "ra_offsets[0]" not in message and "ra_offsets[2]" not in message, "valid values must not be blamed"

    @pytest.mark.parametrize("value", [MAX_OFFSET_ARCSEC, -MAX_OFFSET_ARCSEC], ids=["+limit", "-limit"])
    def test_the_limit_itself_is_allowed(self, value):
        assert resolve_offsets(str(value), 1, "ra_offsets") == [float(value)]

    @pytest.mark.parametrize(
        "value", [MAX_OFFSET_ARCSEC + 1, -MAX_OFFSET_ARCSEC - 1, 1e9], ids=["just over", "just under", "far over"]
    )
    def test_beyond_the_limit_is_refused(self, value):
        """An offset nudges a target within the field; anything approaching this is a
        slew, and a mistyped value should not drive the mount somewhere unintended."""
        with pytest.raises(ValueError, match="exceeds the") as excinfo:
            resolve_offsets(str(value), 1, "dec_offsets")
        assert "36000" in str(excinfo.value), "the limit must be stated in the units used"
        assert "10 degrees" in str(excinfo.value), "and in degrees, which is how an operator thinks"

    @pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "1e400"], ids=["nan", "NaN", "inf", "-inf", "overflow"])
    def test_non_finite_values_are_refused(self, bad):
        """float() accepts all of these -- "1e400" by silently overflowing to inf -- and
        they would reach mount_offset() as an arcsecond count."""
        with pytest.raises(ValueError, match="not a finite number"):
            resolve_offsets(bad, 1, "ra_offsets")

    def test_sexagesimal_is_explicitly_not_accepted(self):
        """Offsets are arcseconds; only ra_j2000_hours/dec_j2000_degs take sexagesimal."""
        with pytest.raises(ValueError, match="not a number"):
            resolve_offsets("00:00:30", 1, "ra_offsets")
