"""ZWOImager._binned_for_hardware reduces the ROI to the sensor's post-binning dimensions
WITHOUT mutating the caller's ImagerSettings.

Regression for the ZWO ROI-shrink across retries: the backend used to shallow-copy the
settings and divide the shared ROI in place, which leaked back into the caller. Acquisition's
``solve_and_correct`` reuses one ``ImagerSettings`` across its retry loop, so an already-binned
ROI was re-divided every try -- 8288x5644 -> 4144x2822 -> 2072x1410 ... -- halving the frame
each attempt. Pure settings arithmetic; no camera, no SDK calls.
"""

from __future__ import annotations

from common.models.statuses import ImagerRoi, ImagerSettings
from zwo import ZWOImager


def _full_frame_bin2() -> ImagerSettings:
    return ImagerSettings(seconds=3.0, binning=2, save=False, roi=ImagerRoi(x=0, y=0, width=8288, height=5644))


def test_does_not_mutate_the_caller_and_bins_correctly():
    s = _full_frame_bin2()
    out = ZWOImager._binned_for_hardware(s)
    assert (s.roi.width, s.roi.height) == (8288, 5644)  # the caller's ROI is untouched
    assert (out.roi.width, out.roi.height) == (4144, 2822)  # the binned, conditioned frame
    assert out.roi is not s.roi  # a distinct ROI object (deep copy)


def test_reused_settings_do_not_shrink_across_calls():
    # The retry loop reuses ONE settings object. Every call must yield the same hardware frame.
    s = _full_frame_bin2()
    dims = []
    for _ in range(3):
        out = ZWOImager._binned_for_hardware(s)
        dims.append((out.roi.width, out.roi.height))
    assert dims == [(4144, 2822), (4144, 2822), (4144, 2822)]
    assert (s.roi.width, s.roi.height) == (8288, 5644)


def test_conditions_to_mod8_and_mod2():
    s = ImagerSettings(seconds=1.0, binning=2, save=False, roi=ImagerRoi(x=0, y=0, width=8290, height=5646))
    out = ZWOImager._binned_for_hardware(s)
    assert out.roi.width % 8 == 0  # 8290//2 = 4145 -> 4144
    assert out.roi.height % 2 == 0
    assert out.roi.width == 4144
