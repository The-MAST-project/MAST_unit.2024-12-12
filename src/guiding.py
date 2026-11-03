from __future__ import annotations

from threading import Thread
from typing import TYPE_CHECKING

from common.activities import ImagerActivities, UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.utils import function_name
from phd2.limit_frame import derived_guiding_roi
from phd2.phd2 import PHD2Connector
from solving_guider import SolvingGuider

if TYPE_CHECKING:
    from unit import Unit

from common.activities import Activities
from common.config.rois import FcuVersion, SpecRoiConfig
from common.endpoints import Tier, endpoint
from common.interfaces.guiding import GuiderInterface, GuiderTypes
from common.mast_logging import get_logger
from common.models.latest import LatestGuiding, LatestGuidingSkyQuality
from common.models.statuses import GuiderStatus, ImagerSettings

logger = get_logger(__name__)
guider_address_port = ("127.0.0.1", 8001)


class Guider(GuiderInterface):
    #: `FullUnitStatus.latest.guiding`: the latest guide start's record.
    latest_guiding: LatestGuiding | None = None

    @staticmethod
    def valid_guiding_methods():
        from common.config import Config

        unit_conf = Config().get_unit()
        assert unit_conf is not None
        return unit_conf.guider.method

    def __init__(self, unit: "Unit" | None, guider_type: str | None = None):  # type: ignore # noqa: UP037
        from phd2.phd2 import PHD2Connector

        self.unit = unit
        self._backend = None
        valid_guiding_methods = Guider.valid_guiding_methods()

        if guider_type is not None:
            if guider_type not in valid_guiding_methods:
                raise ValueError(
                    f"{function_name()}: bad guider_type argument '{guider_type}' "
                    + f"(valid methods={valid_guiding_methods})"
                )
        elif self.unit is not None and self.unit.unit_conf is not None:
            if self.unit.unit_conf.guider.method not in valid_guiding_methods:
                raise ValueError(
                    f"{function_name()}: bad guider_type configuration '{self.unit.unit_conf.guider.method} "
                    + f"(valid types={valid_guiding_methods})"
                )
            guider_type = self.unit.unit_conf.guider.method
        else:
            unit_conf = Config().get_unit()
            assert unit_conf is not None
            guider_type = unit_conf.guider.method

        Activities.__init__(self)
        if guider_type == "phd2":
            self._backend = PHD2Connector(
                parent=self.unit.imager if self.unit else None,
            )
            self.guider_type = GuiderTypes.Phd2
        elif guider_type == "solving":
            self._backend = SolvingGuider(self.unit)
            self.guider_type = GuiderTypes.Solving

    def __repr__(self):
        return f"Guider(_backend={self._backend.__repr__()})"

    def status(self) -> GuiderStatus:
        return GuiderStatus(
            activities=self.activities,
            activities_verbal=self.activities_verbal,
            backend=self._backend.guider_status() if self._backend else None,
        )

    @property
    def sky_quality_while_guiding(self) -> LatestGuidingSkyQuality | None:
        """The current or last guiding session's sky quality; None from a backend without one."""
        if isinstance(self._backend, PHD2Connector):
            return self._backend.sky_quality_while_guiding
        return None

    def begin_latest_guiding(self) -> LatestGuiding:
        """A fresh `latest.guiding` record, for the guide start about to be made."""
        record = LatestGuiding(backend=self.guider_type.value).begin(plan_id=getattr(self.unit, "plan_owner", None))
        if isinstance(self._backend, PHD2Connector):
            self._backend.adopt_guiding_record(record)
        self.latest_guiding = record
        return record

    def start_guiding(self, record: LatestGuiding | None = None) -> CanonicalResponse:
        """Start the backend guiding, and say whether it agreed to.

        PHD2 refuses with errors -- cannot connect, binning or bpp not matching its profile
        -- and without raising `Guiding`. That answer used to be dropped here, so a caller
        waiting on `Guiding` saw it already clear and took the handover for done.

        ``record`` is the one `endpoint_start_guiding` began before answering; otherwise
        one is begun here.
        """
        if not self._backend:
            return CanonicalResponse(errors=["no guider backend"])
        if record is None:
            record = self.begin_latest_guiding()
        try:
            response = self._backend.start_guiding()
        except Exception as ex:
            record.finish(False, [f"{ex}"])
            raise
        response = response if isinstance(response, CanonicalResponse) else CanonicalResponse_Ok
        if response.failed:
            record.finish(False, response.errors)
        return response

    def start_looping(self):
        if isinstance(self._backend, PHD2Connector):
            self._backend.loop()

    def stop_guiding(self):
        if self._backend is not None:
            self._backend.stop_guiding()
            logger.info("guider bakend guiding stopped")
        if self.latest_guiding is not None:
            self.latest_guiding.finish(True)
        if self.unit:
            self.unit.end_activity(UnitActivities.Guiding)
            logger.info("unit guiding stopped")

    def make_guiding_settings(self, base_folder: str | None = None, save: bool = True) -> ImagerSettings:
        """
        The 'guiding' camera exposure settings are used
        - In the second acquisition phase (stage at 'spec' position)
        - While guiding

        :param base_folder:
        :return: camera settings for guiding exposures
        """
        #
        #   +---- FRAME -----------------------------+
        #   |.................^......................|
        #   |.................|-.margin_vertical.....|
        #   |.................v......................|
        #   |...+-- ROI -------------------------+...|
        #   |...|             ^                  |...|
        #   |...|             |                  |...|
        #   |...|             |                  |./--- margin_horizontal
        #   |...|             v                  |.v.|
        #   |<->|<----------->.<---------------->|<->|
        #   |...|    (fiber_x, fiber_y)          |...|
        #   |...|             ^                  |...|
        #   |...|             |                  |...|
        #   |...|             |                  |...|
        #   |...|             |                  |...|
        #   |...|             v                  |...|
        #   +...+--------------------------------+...|
        #   |.................^......................|
        #   |.................|-.margin_vertical.....|
        #   |.................v......................|
        #   +----------------------------------------+
        #
        unit_conf = Config().get_unit()
        assert unit_conf is not None

        if self.unit and self.unit.imager:
            guiding_conf = unit_conf.guiding
            fcu_version = self.unit.fcu_version
            camera_x_size = self.unit.imager.camera_x_size
            camera_y_size = self.unit.imager.camera_y_size
            if camera_x_size is None or camera_y_size is None:
                raise Exception(f"Cannot make guiding settings - bad camera size(s) {camera_x_size=}, {camera_y_size=}")
        else:
            from common import asi

            camera_x_size = asi.ASI_294MM_WIDTH
            camera_y_size = asi.ASI_294MM_HEIGHT
            guiding_conf = unit_conf.guiding
            fcu_version = FcuVersion("fcu_v1")

        cfg = guiding_conf.rois[fcu_version]
        if not isinstance(cfg, SpecRoiConfig):
            raise ValueError(f"cannot make a guiding ROI from {type(cfg)}")

        return ImagerSettings(
            seconds=guiding_conf.exposure,
            base_folder=base_folder,
            gain=guiding_conf.gain,
            binning=guiding_conf.binning,
            roi=derived_guiding_roi(cfg, camera_x_size, camera_y_size, guiding_conf.binning),
            save=save,
        )

    @endpoint(tier=Tier.OPERATION)
    def endpoint_start_guiding(self):
        """
        Manually start guiding after an acquisition-tuning run (handover_automatically_to_guider
        =False), where the acquisition ended at the SPEC position with the mount still tracking and
        the operator then fine-tuned the pointing with external tools.
        """
        op = function_name()
        if self.unit is None:
            return CanonicalResponse(errors=[f"{op}: no unit"])
        if self.unit.is_active(UnitActivities.Guiding):
            return CanonicalResponse(errors=[f"{op}: already guiding"])
        # SolvingGuider reads unit.acquirer.latest_acquisition; the PHD2 backend does not need it.
        if isinstance(self._backend, SolvingGuider) and (
            self.unit.acquirer is None or self.unit.acquirer.latest_acquisition is None
        ):
            return CanonicalResponse(errors=[f"{op}: no acquisition to guide on (solving backend)"])

        # Keep the mount tracking across a later stop (mirrors the sequence-of-exposures path in unit.py):
        # stop_acquisition_and_guiding only stops tracking when it wasn't tracking before guiding.
        if self.unit.mount is None:
            return CanonicalResponse(errors=["self.unit.mount is None"])

        self.unit.was_tracking_before_guiding = self.unit.mount.is_tracking
        if not self.unit.was_tracking_before_guiding:
            self.unit.mount.start_tracking()

        # start_guiding blocks for the solving backend, so run it on a thread.
        Thread(name="guiding", target=self.start_guiding, args=[self.begin_latest_guiding()]).start()
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    def endpoint_stop_acquisition_and_guiding(self):
        return self.stop_acquisition_and_guiding()

    def stop_acquisition_and_guiding(self):
        """
        Stops any in-progress exposure and/or guiding
        """
        # if not self.connected:
        #     logger.warning('Cannot stop guiding - not-connected')
        #     return

        if self.unit is not None:
            # if not self.unit.is_active(
            #     UnitActivities.Acquiring
            # ) and not self.unit.is_active(UnitActivities.Guiding):
            #     error = "not acquiring or guiding"
            #     logger.error(error)
            #     return CanonicalResponse(errors=[error])

            self.unit.end_activity(UnitActivities.Acquiring)
            self.unit.end_activity(UnitActivities.Guiding)
            # Positioning too. `do_acquire` raises it around the stage moves between
            # phases, so a stop that lands in that window used to leave it set for good --
            # a stale flag that makes the unit look busy to everything that reads status,
            # and exactly the imbalance `test_activity_flag_balance` refuses.
            self.unit.end_activity(UnitActivities.Positioning)

            if self.unit.imager is not None and self.unit.imager.is_active(ImagerActivities.Exposing):
                logger.debug(f"{function_name()}: imager is exposing, stopping exposure ...")
                self.unit.imager.stop_exposure()
                logger.info("stopped exposure")

            if self.unit.mount is not None and not self.unit.was_tracking_before_guiding:
                logger.debug(f"{function_name()}: unit was tracking before guiding, stopping tracking ...")
                self.unit.mount.stop_tracking()
                logger.info("stopped tracking")

            if self.unit.guider and self.unit.guider.is_guiding:
                logger.debug(f"{function_name()}: unit was guiding, stopping guiding ...")
                self.unit.guider.stop_guiding()
                logger.info("stopped guiding")
            if self.latest_guiding is not None:
                self.latest_guiding.finish(True)

            logger.debug(f"{function_name()}: acquisition and guiding stopped")

        return CanonicalResponse_Ok

    def abort(self):
        self.stop_guiding()

    @property
    def is_guiding(self) -> bool:
        if self._backend:
            return self._backend.is_guiding
        return False

    @property
    def is_locked(self) -> bool:
        """Guiding, locked on a star: what a completed handover means.

        Stricter than `is_guiding` for PHD2, which also counts `LostLock` (and feeds
        `status.guiding`, which keeps that meaning). The solving backend has no star lock,
        so for it this is `is_guiding`.
        """
        if isinstance(self._backend, PHD2Connector):
            return self._backend.is_locked
        return self.is_guiding


if __name__ == "__main__":
    import sys

    def test_atexit():
        import atexit
        import traceback

        def on_shutdown():
            print("Interpreter is shutting down.")
            traceback.print_stack()

        atexit.register(on_shutdown)

        guider = Guider(unit=None)
        guider.start_guiding()

    def test_make_guiding_settings():
        import json

        guider = Guider(unit=None)
        settings = guider.make_guiding_settings(base_folder="c:/mast/test")
        print(json.dumps(settings.model_dump(), indent=2))

    test_make_guiding_settings()
    sys.exit(0)
