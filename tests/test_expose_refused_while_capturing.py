"""An exposure PHD2 would refuse is refused by the unit first, with a reason an operator can act on.

PHD2 `2.6.14dev1mastbuild5` answers `capture_single_frame` with "cannot capture single
frame when capture is currently active" whenever its capture loop runs: looping,
calibrating, or a guide session that is paused. Seen on mast01 on 2026-10-19 with
guiding paused. While guiding proper, the unit adopts the guide frame instead, so
only the non-guiding states with an active loop are refused.
"""

from __future__ import annotations

import pytest

try:
    from test_limit_frame_fits_sensor import exposure, rig
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from common.activities import ImagerActivities
from common.models.statuses import ImagerRoi

FULL = ImagerRoi.verbatim(0, 0, 8288, 5644)


@pytest.mark.parametrize("state", ["Paused", "Looping", "Calibrating"])
def test_an_active_capture_loop_refuses_before_exposing(state):
    r = rig()
    r.connector.app_state = state

    response = r.connector.start_exposure(exposure(FULL))

    assert response.errors and state in response.errors[0]
    assert "capture_single_frame" not in r.methods()
    started = [c.args[0] for c in r.connector.parent.start_activity.call_args_list]
    assert ImagerActivities.Exposing not in started


@pytest.mark.parametrize("state", ["Stopped", "Selected", ""])
def test_an_idle_phd2_still_captures(state):
    r = rig()
    r.connector.app_state = state

    response = r.connector.start_exposure(exposure(FULL))

    assert not response.errors
    assert "capture_single_frame" in r.methods()
