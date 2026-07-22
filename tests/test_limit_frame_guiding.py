"""Regression tests for DB-driven PHD2 limit-frame guiding (issue #51).

Drives the real ``PHD2Connector`` methods with mocked collaborators — no PHD2
process, no hardware, no Mongo — and asserts on the exact RPC stream the
connector emits, per the ``phd2.limit_frame`` contract:

- ``enabled: false``       -> ``set_limit_frame`` with ``roi: None`` (full frame)
- ``enabled`` + rectangle  -> that rectangle, verbatim, in unbinned pixels
- ``enabled``, no rectangle -> the fiber/margin-derived guiding ROI, as before
- no DB section at all     -> identical to today's deployed behavior

Also pins that acquisition-time exposures still key off
``ImagerSettings.use_set_limit_frame`` alone (untouched by #51).

Runs in the unit venv (Windows): the import chain is Windows-only today
(``stage.py`` uses pyximc names at module level). Skips cleanly elsewhere.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

try:
    from phd2.phd2 import PHD2Connector
except (ImportError, NameError) as ex:  # NameError: stage.py off-Windows
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from common.config.phd2 import LimitFrameConfig, PHD2Config
from common.interfaces.imager import ImagerRoi, ImagerSettings
from phd2.phd2 import SettleModel

# The fiber/margin-derived guiding ROI make_guiding_settings() produces today
# (values from the 2026-07-08 labcomp2 ZWO bench).
DERIVED_ROI = {"x": 1144, "y": 822, "width": 6000, "height": 4000}
# The explicit rectangle from PR #29's deployment example.
EXPLICIT_RECT = {"x": 3031, "y": 2692, "width": 2000, "height": 400}

LEGACY_PHD2_DOC = {
    "profile": "PWI4+ASI-native,binning=1,bpp=16",
    "settle": {"pixels": 1, "time": 0, "timeout": 0},
    "validation_interval": 0.0,
}


def make_connector(limit_frame: LimitFrameConfig | None = None) -> PHD2Connector:
    """A real PHD2Connector minus the heavy __init__, with a real PHD2Config."""
    p = object.__new__(PHD2Connector)
    p._connected = True
    p.watched_process = None
    p.settle = None
    p.settle_px = 0
    p.lock = threading.Lock()
    p.settling_settings = SettleModel(pixels=1, time=0, timeout=0)
    p.errors = []
    p.app_state = ""
    p.profile_binning = 1
    p.profile_bpp = 16
    p.call = MagicMock(name="call", return_value={})
    doc = dict(LEGACY_PHD2_DOC)
    if limit_frame is not None:
        doc["limit_frame"] = limit_frame.model_dump()
    p.conf = PHD2Config(**doc)

    guiding_settings = ImagerSettings(
        seconds=3.0, binning=1, gain=170, format="raw16",
        roi=ImagerRoi(**DERIVED_ROI), image_path="unused",
    )
    guider = MagicMock(name="guider")
    guider.make_guiding_settings.return_value = guiding_settings
    unit = MagicMock(name="unit")
    unit.guider = guider
    parent = MagicMock(name="parent")
    parent.unit = unit
    p.parent = parent
    return p


def rpc_methods(p: PHD2Connector) -> list[str]:
    return [c.args[0] if c.args else c.kwargs.get("method") for c in p.call.call_args_list]


def limit_frame_rois(p: PHD2Connector) -> list[list[int] | None]:
    return [
        c.kwargs["params"]["roi"]
        for c in p.call.call_args_list
        if (c.args and c.args[0] == "set_limit_frame")
    ]


def guiding_settings_of(p: PHD2Connector) -> ImagerSettings:
    return p.parent.unit.guider.make_guiding_settings.return_value


class TestStartGuidingLimitFrame:
    def test_disabled_resets_to_full_frame(self):
        """enabled: false == what operators previously hand-patched phd2.py for."""
        p = make_connector(LimitFrameConfig(enabled=False))
        p.start_guiding()
        assert limit_frame_rois(p) == [None]
        assert guiding_settings_of(p).use_set_limit_frame is False
        assert "guide" in rpc_methods(p)

    def test_explicit_rectangle_sent_verbatim(self):
        p = make_connector(LimitFrameConfig(enabled=True, **EXPLICIT_RECT))
        p.start_guiding()
        r = EXPLICIT_RECT
        assert limit_frame_rois(p) == [[r["x"], r["y"], r["width"], r["height"]]]
        assert guiding_settings_of(p).use_set_limit_frame is True

    def test_enabled_without_rectangle_uses_derived_roi(self):
        p = make_connector(LimitFrameConfig(enabled=True))
        p.start_guiding()
        r = DERIVED_ROI
        assert limit_frame_rois(p) == [[r["x"], r["y"], r["width"], r["height"]]]

    def test_absent_db_section_preserves_deployed_behavior(self):
        """The safe-to-land invariant: no DB entry == today's default exactly."""
        p = make_connector()  # legacy doc, no limit_frame section
        p.start_guiding()
        r = DERIVED_ROI
        assert limit_frame_rois(p) == [[r["x"], r["y"], r["width"], r["height"]]]
        assert guiding_settings_of(p).use_set_limit_frame is True

    def test_limit_frame_set_before_guide_rpc(self):
        """PHD2 must have the frame before star selection starts."""
        p = make_connector(LimitFrameConfig(enabled=True, **EXPLICIT_RECT))
        p.start_guiding()
        methods = rpc_methods(p)
        assert methods.index("set_limit_frame") < methods.index("guide")


class TestSetLimitFrameRpc:
    def test_roi_encodes_as_flat_list_and_arms_reset(self):
        p = make_connector()
        p.set_limit_frame(roi=ImagerRoi(**EXPLICIT_RECT))
        r = EXPLICIT_RECT
        assert limit_frame_rois(p) == [[r["x"], r["y"], r["width"], r["height"]]]
        assert p.need_to_reset_limit_frame is True

    def test_none_resets_and_disarms(self):
        p = make_connector()
        p.set_limit_frame(roi=None)
        assert limit_frame_rois(p) == [None]
        assert p.need_to_reset_limit_frame is False


class TestAcquisitionPathUntouched:
    """start_exposure() keys off ImagerSettings.use_set_limit_frame alone;
    the phd2.limit_frame config must play no role there (#51 scope pin)."""

    def _expose(self, p: PHD2Connector, use_set_limit_frame: bool) -> None:
        p.parent = None  # skip activity bookkeeping
        p.image_was_saved = False
        settings = ImagerSettings(
            seconds=1.0, binning=1, gain=170, format="raw16",
            roi=ImagerRoi(**DERIVED_ROI), image_path="unused",
            use_set_limit_frame=use_set_limit_frame,
        )
        p.start_exposure(settings)

    def test_exposure_respects_settings_flag_despite_config_rect(self):
        p = make_connector(LimitFrameConfig(enabled=True, **EXPLICIT_RECT))
        self._expose(p, use_set_limit_frame=False)
        assert limit_frame_rois(p) == [None]
        assert "capture_single_frame" in rpc_methods(p)

    def test_exposure_uses_settings_roi_not_config_rect(self):
        p = make_connector(LimitFrameConfig(enabled=True, **EXPLICIT_RECT))
        self._expose(p, use_set_limit_frame=True)
        r = DERIVED_ROI
        assert limit_frame_rois(p) == [[r["x"], r["y"], r["width"], r["height"]]]
