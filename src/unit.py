import datetime
import io
import ipaddress
import logging
import os
import socket
import threading
import time
from enum import Enum
from itertools import chain
from pathlib import Path
from threading import Thread
from typing import Annotated, Any

import numpy as np
from fastapi import Query
from fastapi.routing import APIRouter
from PIL import Image
from starlette.websockets import WebSocket, WebSocketDisconnect

from acquirer import Acquirer
from acquisition import Acquisition
from autofocusing import Autofocuser, AutofocusResult
from common import asi
from common.activities import (
    CoverActivities,
    FocuserActivities,
    ImagerActivities,
    MountActivities,
    StageActivities,
    UnitActivities,
)
from common.api import ControllerApi
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.config.rois import FcuVersion
from common.config.unit import UnitConfig
from common.const import Const
from common.dlipowerswitch import PowerSwitchFactory, SwitchedOutlet
from common.endpoints import Tier, add_api_route, endpoint
from common.filer import Filer, MoveGuardian
from common.interfaces.components import Component

# from guiding import Guider
from common.interfaces.imager import ImagerExposureSeries, ImagerRoi, ImagerSettings, ImagerTypes
from common.mast_logging import DailyFileHandler, get_logger
from common.models.assignments import AssignmentNotification, UnitAssignment
from common.models.statuses import FullUnitStatus, StatusType
from common.notifications import Notifier
from common.parsers import (
    DEC_PATTERN,
    RA_PATTERN,
    sexagesimal_degrees_to_decimal,
    sexagesimal_hours_to_decimal,
)
from common.paths import PathMaker
from common.rois import UnitRoi
from common.utils import RepeatTimer, function_name, time_stamp
from covers import Covers
from expose_params import MAX_OFFSET_ARCSEC, MAX_OFFSET_DEGREES, resolve_exposure_roi, resolve_offsets
from focuser import Focuser
from imagers import Imager
from mount import Mount, SettleMode
from PlaneWave import pwi4_client
from solving import Solver
from stage import Stage

logger = get_logger(__name__)
filer = Filer(logger)

AUTOFOCUS_STOP_TIMEOUT_SECONDS = 30.0


def configured_imager() -> ImagerTypes | None:
    unit_conf = Config().get_unit()
    if not unit_conf:
        return None
    t = unit_conf.imager.imager_type
    if t.startswith("ascom"):
        t = "ascom"
    return ImagerTypes(t)


# def get_imager_type(
#     imager_type: ImagerTypes = Query(default=configured_imager()),
# ) -> ImagerTypes:
#     t = imager_type
#     if not t:
#         t = configured_imager()
#         assert t is not None, "No imager type configured"
#     return t


class GuideDirections(Enum):
    guide_north = 0
    guide_south = 1
    guide_east = 2
    guide_west = 3


class Unit(Component):
    MAX_UNITS = 20
    MAX_AUTOFOCUS_TRIES = 3

    _instance = None
    _initialized = False
    unit_shutdown_event: threading.Event = threading.Event()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # logger.info(f"Unit.__new__: allocated instance 0x{id(cls._instance):x}")
        return cls._instance

    def __init__(self):
        from guiding import Guider

        if self._initialized:
            return
        # logger.info(f"Unit.__init__: initiating instance 0x{id(self):x}")

        Component.__init__(self, UnitActivities)

        self._connected: bool = False

        self.was_tracking_before_guiding: bool = False

        # Handlers live on the root logger now, not on this one -- look there.
        file_handlers = [h for h in logging.getLogger().handlers if isinstance(h, DailyFileHandler)]
        if file_handlers:
            logger.info(f"logging to '{file_handlers[0].path}'")

        self._init_errors: list[str] = []

        self.unit_conf: UnitConfig | None = None
        try:
            self.unit_conf = Config().get_unit()
        except Exception as ex:
            msg = f"unit configuration failed to load: {ex}"
            logger.exception(msg)
            self._init_errors.append(msg)
        if self.unit_conf is None and not self._init_errors:
            msg = "unit configuration could not be loaded (no config in database or TOML)"
            logger.error(msg)
            self._init_errors.append(msg)

        if self.unit_conf is not None:
            self.min_ra_correction_arcsec = self.unit_conf.guiding.min_ra_correction_arcsec
            self.min_dec_correction_arcsec = self.unit_conf.guiding.min_dec_correction_arcsec
            self.autofocus_max_tolerance = self.unit_conf.autofocus.max_tolerance
        else:
            self.min_ra_correction_arcsec = 0.0
            self.min_dec_correction_arcsec = 0.0
            self.autofocus_max_tolerance = 0.0
        self.autofocus_try: int = 0

        self.hostname = socket.gethostname()

        def _try_init(name: str, factory):
            try:
                return factory()
            except Exception as ex:
                msg = f"component '{name}' failed to initialize: {ex}"
                logger.exception(msg)
                self._init_errors.append(msg)
                return None

        self.power_switch = _try_init("power_switch", PowerSwitchFactory.get_instance)
        self.mount: Mount | None = _try_init("mount", lambda: Mount(self))
        self.imager: Imager | None = _try_init("imager", lambda: Imager(self))
        self.covers: Covers | None = _try_init("covers", lambda: Covers(self))
        self.stage: Stage | None = _try_init("stage", lambda: Stage(self))
        self.focuser: Focuser | None = _try_init("focuser", lambda: Focuser(self))
        self.pw: pwi4_client.PWI4 | None = _try_init("pw", pwi4_client.PWI4)
        self.autofocuser: Autofocuser | None = _try_init("autofocuser", lambda: Autofocuser(self))
        self.solver: Solver | None = _try_init("solver", lambda: Solver(self))
        self.acquirer: Acquirer | None = _try_init("acquirer", lambda: Acquirer(self))
        self.guider: Guider | None = _try_init("guider", lambda: Guider(self))

        self.components: list[Component] = [
            c
            for c in [
                self.power_switch,
                self.mount,
                self.imager,
                self.covers,
                self.focuser,
                self.stage,
            ]
            if c is not None
        ]

        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = "unit-timer-thread"
        self.timer.start()

        self.reference_image: np.ndarray | None = None
        self.autofocus_result: AutofocusResult | None = None

        self._was_shut_down = False

        self.connected_clients: list[WebSocket] = []
        # self.imager.register_visualizer('image-to-dashboard', self.push_image_to_dashboards)

        self.controller_api = _try_init("controller_api", ControllerApi)

        self.errors: list[str] = list(self._init_errors)

        self.spirals_folder: str | None = None
        self.spiral_exposure_series: ImagerExposureSeries | None = None
        self.latest_acquisition: Acquisition | None = None

        self._initialized = True
        logger.info("unit: initialized")

    @property
    def fcu_version(self) -> FcuVersion:
        assert self.stage and self.stage.fcu_version
        return FcuVersion(self.stage.fcu_version)

    def do_startup(self):
        self.start_activity(UnitActivities.StartingUp)
        [comp.startup() for comp in self.components]

    @endpoint(tier=Tier.CONTRACT)
    def endpoint_startup(self):
        return self.startup()

    def startup(self):
        """
        Starts the **MAST** ``unit`` subsystem.  Makes it ``operational``.
        """
        if self.is_active(UnitActivities.StartingUp):
            return

        self._was_shut_down = False
        Thread(name="unit-startup-thread", target=self.do_startup).start()
        return CanonicalResponse_Ok

    def do_shutdown(self):
        self.start_activity(UnitActivities.ShuttingDown)
        [comp.shutdown() for comp in self.components]
        if self.guider:
            self.guider.abort()

        self._was_shut_down = True
        self.timer.cancel()
        self.unit_shutdown_event.set()

    @property
    def is_shutting_down(self) -> bool:
        return self.is_active(UnitActivities.ShuttingDown)

    def powerdown(self):
        """
        Powers down the unit by shutting down and then turning off all power sockets.
        """
        if not self._was_shut_down:
            self.shutdown()
        while self.is_shutting_down:
            time.sleep(0.5)
        self.power_all_off()
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.CONTRACT)
    def endpoint_shutdown(self):
        return self.shutdown()

    def shutdown(self):
        """
        Shuts down the **MAST** ``unit`` subsystem.  Makes it ``idle``.
        """
        if not self.connected:
            self.connect()

        if self.is_active(UnitActivities.ShuttingDown):
            return

        Thread(name="shutdown-thread", target=self.do_shutdown).start()
        return CanonicalResponse_Ok

    @property
    def connected(self):
        return all(comp.connected for comp in self.components)

    @connected.setter
    def connected(self, value: bool):
        """
        Should connect/disconnect anything that needs connecting/disconnecting
        """
        if self.mount:
            self.mount.connected = value
        if self.imager:
            self.imager.connected = value
        if self.covers:
            self.covers.connected = value
        if self.stage:
            self.stage.connected = value
        if self.focuser:
            self.focuser.connected = value

    def connect(self):
        """
        Connects the **MAST** ``unit`` subsystems to all its ancillaries.
        """
        self.connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        Disconnects the **MAST** ``unit`` subsystems from all its ancillaries.
        """
        self.connected = False
        return CanonicalResponse_Ok

    def power_all_on(self):
        """
        Turn **ON** all power sockets
        """
        for c in self.components:
            if isinstance(c, SwitchedOutlet):
                c.power_on()

    def power_all_off(self):
        """
        Turn **OFF** all power sockets
        """
        for c in self.components:
            c.powerdown()

    @endpoint(tier=Tier.CONTRACT)
    def endpoint_status(self) -> Any:
        # Enveloped at registration; `status()` stays a bare FullUnitStatus, which
        # MAST_common#70 requires -- its fields are typed as the component status models.
        #
        # `serialize_ip_addresses` is kept as-is, deliberately: it is a no-op on a Pydantic
        # model (it handles dict / list / IPv4Address and falls through on everything else),
        # so it almost certainly does nothing here. Removing it is a separate question from
        # moving the envelope, and this pass changes no behaviour.
        return serialize_ip_addresses(self.status())

    def status(self) -> FullUnitStatus:
        autofocus = (
            {
                "success": self.autofocus_result.success,
                "best_position": self.autofocus_result.best_position,
                "tolerance": self.autofocus_result.tolerance,
                "time_stamp": self.autofocus_result.time_stamp,
            }
            if self.autofocus_result
            else None
        )

        all_corrections: list = []

        # if self.acquirer and self.acquirer.latest_acquisition:
        #     corrections_list = (
        #         self.acquirer.latest_acquisition.corrections
        #         if (self.acquirer.latest_acquisition and self.acquirer.latest_acquisition.corrections)
        #         else []
        #     )

        #     for phase in list(get_args(Const.CorrectionPhase)):
        #         if isinstance(corrections_list, dict):
        #             if phase not in corrections_list:
        #                 corrections_list[phase] = Corrections(phase=phase)
        #             correction = corrections_list[phase]
        #             if isinstance(correction, list):
        #                 all_corrections.extend(correction)
        #             else:
        #                 all_corrections.append(correction)

        ret = FullUnitStatus(
            **self.component_status().model_dump(),
            id=id(self),
            powered=True,
            guiding=self.guider.is_guiding if self.guider else False,
            autofocusing=self.autofocuser.is_autofocusing if self.autofocuser else False,
            power_switch=self.power_switch.status() if self.power_switch else None,
            mount=self.mount.status() if self.mount else None,
            imager=self.imager.status() if self.imager else None,  # type: ignore
            covers=self.covers.status() if self.covers else None,
            focuser=self.focuser.status() if self.focuser else None,
            stage=self.stage.status() if self.stage else None,
            guider=self.guider.status() if self.guider else None,  # type: ignore
            # solver= self.solver.status(),
            errors=self.errors,
            autofocus=autofocus,
            corrections=all_corrections,
            date=time_stamp(),
        )
        ret.type = StatusType.FULL  # Should already be set in the constructor, but WAS NOT, so setting it explicitly here.

        return ret

    @staticmethod
    def quit():
        """
        Quits the application
        """
        from app import app_quit

        app_quit(reason="quit()")

    @endpoint(tier=Tier.CONTRACT)
    def endpoint_abort(self):
        return self.abort()

    def abort(self):
        """
        Aborts any in-progress activities
        """

        if self.autofocuser is not None and (
            self.is_active(UnitActivities.AutofocusingPWI4) or self.is_active(UnitActivities.Autofocusing)
        ):
            self.autofocuser.stop_autofocus()
            for flag in (UnitActivities.AutofocusingPWI4, UnitActivities.Autofocusing):
                if not self.await_activity_clear(flag, timeout=AUTOFOCUS_STOP_TIMEOUT_SECONDS):
                    msg = (
                        f"{function_name()}: autofocus did not stop within "
                        f"{AUTOFOCUS_STOP_TIMEOUT_SECONDS} seconds ({flag!r} still set)"
                    )
                    return CanonicalResponse(errors=[msg])

        if self.guider:
            self.guider.abort()
        [comp.abort() for comp in self.components]
        return CanonicalResponse_Ok

    def ontimer(self):  # noqa: C901
        if self.unit_shutdown_event.is_set():
            self.timer.cancel()
            return

        """
        Used in order to end activities that were started elsewhere in the code.
        """
        # UnitActivities.StartingUp
        if self.is_active(UnitActivities.StartingUp) and not (
            (self.mount and self.mount.is_active(MountActivities.StartingUp))
            or (self.imager and self.imager.is_active(ImagerActivities.StartingUp))
            or (self.stage and self.stage.is_active(StageActivities.StartingUp))
            or (self.focuser and self.focuser.is_active(FocuserActivities.StartingUp))
            or (self.covers and self.covers.is_active(CoverActivities.StartingUp))
        ):
            self.end_activity(UnitActivities.StartingUp)

        # UnitActivities.ShuttingDown
        if self.is_active(UnitActivities.ShuttingDown) and not (
            (self.mount and self.mount.is_active(MountActivities.ShuttingDown))
            or (self.imager and self.imager.is_active(ImagerActivities.ShuttingDown))
            or (self.stage and self.stage.is_active(StageActivities.ShuttingDown))
            or (self.focuser and self.focuser.is_active(FocuserActivities.ShuttingDown))
            or (self.covers and self.covers.is_active(CoverActivities.ShuttingDown))
        ):
            self.end_activity(UnitActivities.ShuttingDown)
            self._was_shut_down = True

        # UnitActivities.AutofocusingPWI4
        if self.pw is not None and self.autofocuser is not None and self.is_active(UnitActivities.AutofocusingPWI4):
            autofocus_status = self.pw.status().autofocus
            if not autofocus_status:
                logger.error("Empty PWI4 autofocus status")
            elif not autofocus_status.is_running:  # type: ignore # it's done
                logger.info("PWI4 autofocus ended, getting status.")
                self.autofocus_result = AutofocusResult()
                self.autofocus_result.success = autofocus_status.success  # type: ignore
                if self.autofocus_result.success:
                    self.autofocus_result.best_position = autofocus_status.best_position  # type: ignore
                    self.autofocus_result.tolerance = autofocus_status.tolerance  # type: ignore

                    best_position = autofocus_status.best_position  # type: ignore
                    assert self.unit_conf is not None
                    self.unit_conf.focuser.known_as_good_position = best_position
                    try:
                        Config().set_unit(site_name=None, unit_name=None, unit_conf=self.unit_conf)
                        logger.info(f"autofocus: saved {best_position=} in the configuration for unit {self.hostname}.")
                        if autofocus_status.tolerance > self.autofocus_max_tolerance:  # type: ignore
                            if self.autofocus_try < Unit.MAX_AUTOFOCUS_TRIES:
                                self.autofocus_try += 1
                                logger.info(
                                    f"autofocus: latest {autofocus_status.tolerance=} greater than"  # type: ignore
                                    + f"{self.autofocus_max_tolerance=}, starting autofocus "
                                    + f"try #{self.autofocus_try}"
                                )
                                self.autofocuser.start_pwi4_autofocus()
                            else:
                                logger.info(
                                    f"autofocus: failed to reach {self.autofocus_max_tolerance=} "
                                    + f"in {Unit.MAX_AUTOFOCUS_TRIES=}"
                                )
                        else:
                            self.autofocus_try = 0

                    except Exception as e:
                        logger.exception(
                            "failed to save unit_conf for ['focuser']['know_as_good_position']",
                            exc_info=e,
                        )
                else:
                    logger.error("PlaneWave autofocus failed")
                    self.autofocus_result.best_position = None
                    self.autofocus_result.tolerance = None
                self.autofocus_result.time_stamp = datetime.datetime.now(tz=datetime.UTC).isoformat()

                self.end_activity(UnitActivities.AutofocusingPWI4)
            else:
                logger.info(f"PlaneWave autofocus in progress {self.autofocus_try=}")

    def end_lifespan(self):
        logger.info("unit end lifespan")
        self.shutdown()

    def start_lifespan(self):
        logger.debug("unit start lifespan")
        self.startup()

    @property
    def operational(self) -> bool:
        if self._init_errors:
            return False
        components = set(self.components)
        if self.unit_conf and self.unit_conf.name.lower() == "mastw":
            components.discard(self.covers)
        return all(c.operational for c in components)

    @property
    def why_not_operational(self) -> list[str]:
        if self._init_errors:
            return list(self._init_errors)
        components = set(self.components)
        if self.unit_conf and self.unit_conf.name.lower() == "mastw":
            components.discard(self.covers)
        return list(chain.from_iterable(c.why_not_operational for c in components))

    @property
    def name(self) -> str:
        return "unit"

    @property
    def detected(self) -> bool:
        # return all([comp.detected for comp in self.components])
        return True

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    async def unit_visual_ws(self, websocket: WebSocket):
        logger.info(f"accepting on {websocket=} ...")
        await websocket.accept()
        self.connected_clients.append(websocket)
        logger.info(f"added {websocket} to self.connected_clients")
        try:
            while True:
                _ = await websocket.receive_text()
        except WebSocketDisconnect:
            self.connected_clients.remove(websocket)
            logger.info(f"removed {websocket} from self.connected_clients")

    async def push_image_to_dashboards(self, image: np.ndarray):
        transposed_image = np.transpose(image.astype(np.uint16))
        image_pil = Image.fromarray(transposed_image)
        with io.BytesIO() as output:
            image_pil.save(output, format="PNG")
            png_data = output.getvalue()

        for websocket in self.connected_clients:
            try:
                logger.info(f"pushing to {websocket.url=} ...")
                await websocket.send_bytes(png_data)
                # loop = asyncio.get_event_loop()
                # loop.run_until_complete(websocket.send(png_data))
            except Exception:
                logger.exception("websocket.send error")

    @endpoint(tier=Tier.OPERATION)
    def expose(
        self,
        ra_j2000_hours: Annotated[
            str | float | None,
            Query(
                pattern=RA_PATTERN,
                description=(
                    "### Right Ascension (J2000) in either:\n"
                    "- decimal hours (e.g., `12.5`) or\n"
                    "- sexagesimal format (e.g., `12:30:45.123`). \n"
                    "- Decimal range: `0 <= RA < 24`.\n"
                    "If not supplied, taken from telescope"
                ),
            ),
        ] = None,
        dec_j2000_degs: Annotated[
            str | float | None,
            Query(
                pattern=DEC_PATTERN,
                description=(
                    "### Declination (J2000) in either:\n"
                    "- decimal degrees (e.g., `-45.5`) or\n"
                    "- sexagesimal format (e.g., `-45:30:00.123`). \n"
                    "- Decimal range: `-90 <= DEC <= 90`.\n"
                    "If not supplied, taken from telescope"
                ),
            ),
        ] = None,
        subfolder: str | None = None,
        exposure_seconds: float = 3,
        repeats: int = 1,
        seconds_between_exposures: float = 0,
        fiber_x: int | None = None,
        fiber_y: int | None = None,
        width: int | None = None,
        height: int | None = None,
        binning: asi.ASI_294MM_SUPPORTED_BINNINGS_LITERAL = 1,
        gain: int = asi.ASI_294MM_DEFAULT_GAIN,
        ra_offsets: Annotated[
            str | list[str] | list[float] | None,
            Query(
                description=(
                    "#### Optional RA offsets applied between exposures.\n"
                    "**Arcseconds, as plain decimals.**\n"
                    "- omitted or empty - no RA offsetting\n"
                    "- one value - used after every repeat (e.g. `1.5`)\n"
                    "- exactly `repeats` values - one each (e.g. `1.5 -2 0`)\n"
                    f"- each value must be within **±{MAX_OFFSET_ARCSEC:g} arcsec "
                    f"(±{MAX_OFFSET_DEGREES}°)**; to move further, slew with "
                    "`ra_j2000_hours`/`dec_j2000_degs`"
                ),
            ),
        ] = None,
        dec_offsets: Annotated[
            str | list[str] | list[float] | None,
            Query(
                description=(
                    "#### Optional DEC offsets applied between exposures.\n"
                    "**Arcseconds, as plain decimals.**\n"
                    "- omitted or empty - no DEC offsetting\n"
                    "- one value - used after every repeat (e.g. `1.5`)\n"
                    "- exactly `repeats` values - one each (e.g. `1.5 -2 0`)\n"
                    f"- each value must be within **±{MAX_OFFSET_ARCSEC:g} arcsec "
                    f"(±{MAX_OFFSET_DEGREES}°)**; to move further, slew with "
                    "`ra_j2000_hours`/`dec_j2000_degs`"
                ),
            ),
        ] = None,
    ) -> CanonicalResponse:

        if self.imager is None:
            return CanonicalResponse(errors=["imager is not initialized"])

        # Both or neither. A coordinate on its own used to be accepted and then quietly
        # dropped: the slew below requires BOTH to be floats, so supplying only RA meant
        # no slew, no error, and a caller believing it had pointed somewhere it had not.
        if (ra_j2000_hours is None) != (dec_j2000_degs is None):
            given, missing = (
                ("ra_j2000_hours", "dec_j2000_degs") if dec_j2000_degs is None else ("dec_j2000_degs", "ra_j2000_hours")
            )
            return CanonicalResponse(
                errors=[
                    (
                        f"expose: {given} was supplied without {missing}; "
                        "supply both to slew, or neither to expose where the telescope is pointing"
                    )
                ]
            )

        # One call, whatever the form -- see the note in acquirer.py. This endpoint's
        # pattern was the colon-only copy, so the space-separated form was rejected
        # here while acquirer accepted it; both now share RA_PATTERN/DEC_PATTERN.
        if ra_j2000_hours:
            try:
                ra_j2000_hours = sexagesimal_hours_to_decimal(ra_j2000_hours)
            except ValueError as e:
                return CanonicalResponse(errors=[f"expose: bad ra_j2000_hours '{ra_j2000_hours}' -- {e}"])

        if dec_j2000_degs:
            try:
                dec_j2000_degs = sexagesimal_degrees_to_decimal(dec_j2000_degs)
            except ValueError as e:
                return CanonicalResponse(errors=[f"expose: bad dec_j2000_degs '{dec_j2000_degs}' -- {e}"])

        assert self.mount is not None
        if (ra_j2000_hours is not None and isinstance(ra_j2000_hours, float)) and (
            dec_j2000_degs is not None and isinstance(dec_j2000_degs, float)
        ):
            logger.info(f"slewing mount to ra={ra_j2000_hours}, dec={dec_j2000_degs}")
            self.mount.goto_ra_dec_j2000(ra=ra_j2000_hours, dec=dec_j2000_degs)
            self.mount.wait_until_settled(SettleMode.SLEW)

        try:
            ra_offsets = resolve_offsets(ra_offsets, repeats, "ra_offsets")
            dec_offsets = resolve_offsets(dec_offsets, repeats, "dec_offsets")
        except ValueError as e:
            return CanonicalResponse(errors=[f"expose: {e}"])

        try:
            fiber_x, fiber_y, width, height = resolve_exposure_roi(
                fiber_x, fiber_y, width, height, self.imager.camera_x_size, self.imager.camera_y_size
            )
        except ValueError as e:
            return CanonicalResponse(errors=[f"expose: {e}"])

        Thread(
            name="expose-thread",
            target=self.do_expose,
            args=[
                subfolder,
                exposure_seconds,
                repeats,
                ra_offsets,
                dec_offsets,
                seconds_between_exposures,
                fiber_x,
                fiber_y,
                width,
                height,
                binning,
                gain,
            ],
        ).start()
        return CanonicalResponse_Ok

    def do_expose(
        self,
        subfolder: str | None = None,
        exposure_seconds: float = 3,
        repeats: int = 1,
        ra_offsets: list[float] | None = None,
        dec_offsets: list[float] | None = None,
        seconds_between_exposures: float = 0,
        fiber_x: int = 6000,
        fiber_y: int = 2500,
        width: int = 1500,
        height: int = 1300,
        binning: asi.ASI_294MM_SUPPORTED_BINNINGS_LITERAL = 1,
        gain: int = asi.ASI_294MM_DEFAULT_GAIN,
    ) -> CanonicalResponse:

        assert self.mount is not None
        assert self.imager is not None

        op = function_name()
        seconds = exposure_seconds

        self.mount.start_tracking()
        exposure_series = self.imager.start_exposure_series(purpose="unit.do_exposure")
        try:
            self._expose_repeatedly(
                repeats,
                seconds,
                subfolder,
                gain,
                binning,
                fiber_x,
                fiber_y,
                width,
                height,
                ra_offsets,
                dec_offsets,
                seconds_between_exposures,
            )
        except Exception:
            # This runs in `expose-thread`, where an exception would otherwise vanish
            # entirely -- the endpoint has already returned "ok" to the caller. Logging
            # is the only trace there is; the finally below is what stops the mount
            # tracking forever and the exposure series dangling.
            logger.exception(f"{op}: exposure run failed")
            return CanonicalResponse(errors=[f"{op}: exposure run failed, see the log"])
        finally:
            self.imager.end_exposure_series(exposure_series)
            self.mount.stop_tracking()
        return CanonicalResponse_Ok

    def _expose_repeatedly(
        self,
        repeats: int,
        seconds: float,
        subfolder: str | None,
        gain: int,
        binning: asi.ASI_294MM_SUPPORTED_BINNINGS_LITERAL,
        fiber_x: int,
        fiber_y: int,
        width: int,
        height: int,
        ra_offsets: list[float] | None,
        dec_offsets: list[float] | None,
        seconds_between_exposures: float,
    ) -> None:
        assert self.mount is not None
        assert self.imager is not None
        op = function_name()
        for repeat in range(repeats):
            end = None
            if seconds_between_exposures != 0.0:
                start = datetime.datetime.now(tz=datetime.UTC)
                end = start + datetime.timedelta(seconds=seconds_between_exposures)

            unit_roi = UnitRoi(fiber_x, fiber_y, width, height)
            default_folder = PathMaker().make_exposures_folder()
            base_folder = os.path.join(default_folder, subfolder) if subfolder else default_folder
            imager_settings = ImagerSettings(
                seconds=seconds,
                base_folder=base_folder,
                gain=gain,
                binning=binning,
                # roi=unit_roi.to_imager_roi(binning=imager_binning),
                roi=ImagerRoi.from_other(roi=unit_roi).binned(binning),
                tags={"roi": None},
                save=True,
            )
            self.imager.latest_settings = imager_settings

            logger.info(f"{op}: starting exposure #{repeat} (of {repeats})")
            # image_path is already set: ImagerSettings.model_post_init calls
            # make_file_name() when base_folder is given, so the name exists before the
            # exposure starts -- which is what lets the file be protected while it is
            # being written. protect() also marks it a product, so a release_folder on
            # the containing folder cannot discard it.
            image_path = imager_settings.image_path
            if image_path is None:
                self.imager.start_exposure(imager_settings)
            else:
                with MoveGuardian().protect(image_path):
                    self.imager.start_exposure(imager_settings)
                    self.imager.wait_for_image_saved()
                filer.move_ram_to_shared(image_path)

            if end is not None and seconds_between_exposures != 0.0:
                now = datetime.datetime.now(tz=datetime.UTC)
                if now < end:
                    period = (end - now).seconds
                    logger.info(f"{op}: sleeping {period} seconds till next exposure ...")
                    time.sleep(period)

            if ra_offsets is not None or dec_offsets is not None:
                if ra_offsets is not None and dec_offsets is not None:
                    logger.info(f"offsetting mount ra={ra_offsets[repeat]}, dec={dec_offsets[repeat]}")
                    self.mount.pw.mount_offset(
                        ra_add_arcsec=ra_offsets[repeat],
                        dec_add_arcsec=dec_offsets[repeat],
                    )
                elif ra_offsets is not None:
                    logger.info(f"offsetting mount ra={ra_offsets[repeat]}")
                    self.mount.pw.mount_offset(ra_add_arcsec=ra_offsets[repeat])
                elif dec_offsets is not None:
                    logger.info(f"offsetting mount dec={dec_offsets[repeat]}")
                    self.mount.pw.mount_offset(dec_add_arcsec=dec_offsets[repeat])
                self.mount.wait_until_settled(SettleMode.OFFSET_STEP)
        # Closing the series and stopping tracking belong to do_expose's `finally`, so
        # that they happen whether or not this returns normally. They must NOT be here.

    @endpoint(tier=Tier.OPERATION)
    def endpoint_test_stage_repeatability(
        self,
        start_position: int | str = 50000,
        end_position: int | str = 300000,
        step: int | str = 25000,
        exposure_seconds: int | str = 5,
        binning: int | str = 1,
        gain: int | str = asi.ASI_294MM_DEFAULT_GAIN,
    ) -> CanonicalResponse:
        Thread(
            name="test-stage-repeatability",
            target=self.do_test_stage_repeatability,
            args=[start_position, end_position, step, exposure_seconds, binning, gain],
        ).start()
        return CanonicalResponse_Ok

    def do_test_stage_repeatability(  # noqa: C901
        self,
        start_position: int | str = 50000,
        end_position: int | str = 300000,
        step: int | str = 25000,
        exposure_seconds: int | str = 5,
        binning: asi.ASI_294MM_SUPPORTED_BINNINGS_LITERAL = 1,
        gain: int | str = asi.ASI_294MM_DEFAULT_GAIN,
    ) -> CanonicalResponse:

        assert self.imager is not None
        assert self.stage is not None

        op = function_name()

        if isinstance(start_position, str):
            start_position = int(start_position)
        if isinstance(end_position, str):
            end_position = int(end_position)
        if isinstance(step, str):
            step = int(step)
        if isinstance(exposure_seconds, str):
            exposure_seconds = int(exposure_seconds)
        if isinstance(gain, str):
            gain = int(gain)

        reference_position = start_position

        repeatablility_exposure_series = self.imager.start_exposure_series("unit.do_test_stage_repeatability")

        for position in range(start_position + step, end_position, step):
            logger.info(f"{op}: moving stage to {reference_position=}")
            self.stage.move_absolute(reference_position)
            while self.stage.is_active(StageActivities.Moving):
                time.sleep(0.5)

            # expose at reference
            exposure_settings = ImagerSettings(
                seconds=exposure_seconds,
                base_folder=PathMaker().make_exposures_folder(),
                gain=gain,
                binning=binning,
                roi=None,
                tags={
                    "stage-repeatability": None,
                    "reference-for": position,
                },
                save=True,
            )

            self.imager.start_exposure(exposure_settings)
            if exposure_settings.image_path is not None:
                self.imager.wait_for_image_saved()
                logger.info(f"{op}: reference image was saved")
                filer.move_ram_to_shared(exposure_settings.image_path)

            # expose at shifted position
            logger.info(f"{op}: moving stage to shifted {position=}")
            self.stage.move_absolute(position)
            while self.stage.is_active(StageActivities.Moving):
                time.sleep(0.5)

            exposure_settings = ImagerSettings(
                seconds=exposure_seconds,
                base_folder=PathMaker().make_exposures_folder(),
                gain=gain,
                binning=binning,
                roi=None,
                tags={
                    "stage-repeatability": None,
                    "position": position,
                },
                save=True,
            )
            self.imager.start_exposure(exposure_settings)
            if exposure_settings.image_path is not None:
                self.imager.wait_for_image_saved()
                logger.info(f"{op}: image at {position=} was saved")
                filer.move_ram_to_shared(exposure_settings.image_path)

        self.imager.end_exposure_series(repeatablility_exposure_series)

        logger.info(f"{op}: done.")
        return CanonicalResponse_Ok

    def do_execute_assignment(self, assignment: UnitAssignment):
        """
        Execute an assignment in a separate Thread
        :param assignment:
        :return:
        """
        assert self.acquirer is not None
        assert self.autofocuser is not None
        assert self.guider is not None
        assert self.imager is not None

        if assignment.plan.autofocus:
            self.autofocuser.start_autofocus(
                ra_j2000_hours=assignment.plan.target.ra_hours,
                dec_j2000_degs=assignment.plan.target.dec_degrees,
            )

            while self.is_active(UnitActivities.Autofocusing):
                time.sleep(10)

            #
            # If UnitActivities.Autofocusing ends in success, an autofocuser result should
            #  be available
            #
            if not self.autofocuser.latest_result:
                return  # should propagate errors as well

            if assignment.plan.ulid is not None and self.imager.latest_settings and self.imager.latest_settings.image_path:
                Notifier().assignment_notification(
                    AssignmentNotification(
                        assignment_id=assignment.plan.ulid,
                        state="in-progress",
                        # Relative to the shared root. This sent `.parent.name` -- the bare
                        # directory name, with no path at all -- so the controller symlinked
                        # something it could never resolve. MAST_spec#39.
                        shared_top=os.path.relpath(Path(self.imager.latest_settings.image_path).parent, filer.ram.root),
                        shared_subpath="autofocus",
                    )
                )

            #
            # At this point we have autofocused and can start acquisition
            #
            self.acquirer.endpoint_start_acquisition_and_guiding(
                ra_j2000_hours=assignment.plan.target.ra_hours,
                dec_j2000_degs=assignment.plan.target.dec_degrees,
            )

            if assignment.plan.ulid is not None and self.acquirer.latest_acquisition is not None:
                Notifier().assignment_notification(
                    AssignmentNotification(
                        assignment_id=assignment.plan.ulid,
                        state="in-progress",
                        # Relative to the shared root, not the absolute ram path: the
                        # controller symlinks this, and its shared root is spelled
                        # differently from ours. MAST_spec#39.
                        shared_top=os.path.relpath(self.acquirer.latest_acquisition.folder, filer.ram.root),
                        shared_subpath="acquisition",
                    )
                )

    @endpoint(tier=Tier.CONTRACT)
    async def endpoint_execute_assignment(self, assignment: UnitAssignment):
        if not self.operational:
            return CanonicalResponse(errors=self.why_not_operational)

        if not self.is_idle():
            return CanonicalResponse(errors=[f"busy ({self.activities=})"])

        Thread(target=self.do_execute_assignment, args=[assignment]).start()

        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    async def endpoint_start_dancing(self, style: str = "foxtrot"):
        logger.info(f"unit.dance: dancing the {style} ...")
        self.start_activity(UnitActivities.Dancing, details=[style])
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    async def endpoint_stop_dancing(self):
        logger.info("unit.dance: stopping dancing ...")
        self.end_activity(UnitActivities.Dancing)
        return CanonicalResponse_Ok

    # async def set_sky_and_spec_pixel_values(self,
    #     sky_x: int, sky_y: int, spec_x: int, spec_y: int
    # ):

    #     cfg = Config().get_unit()
    #     assert self.fcu_version is not None
    #     roi_cfg = cfg.acquisition.rois[FcuVersion(self.fcu_version)]

    #     roi_cfg.sky_x = sky_x
    #     roi_cfg.sky_y = sky_y
    #     cfg.guiding.rois.fiber_x = spec_x
    #     cfg.guiding.rois.fiber_y = spec_y

    #     Config().set_unit(unit_name=cfg.name, unit_conf=cfg)

    #     return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    def endpoint_spiral_new_path(self, x_step_arcsec: float, y_step_arcsec: float):
        """
        Defines a new spiral path<br>
        **NOTE**: Remember to call `spiral_end_path()` when done with the spiral path
        """
        assert self.mount is not None
        assert self.imager is not None

        self.mount.pw.mount_spiral_offset_new(x_step_arcsec=x_step_arcsec, y_step_arcsec=y_step_arcsec)
        self.spirals_folder = PathMaker().make_spirals_folder()

        image_path = os.path.join(
            self.spirals_folder,
            "step-" + PathMaker().make_seq(self.spirals_folder) + ".fits",
        )
        self.imager.latest_settings = ImagerSettings(seconds=5, save=True, image_path=image_path, binning=1)
        self.spiral_exposure_series = self.imager.start_exposure_series(purpose="spiral_new_path")
        self.imager.start_exposure(self.imager.latest_settings)
        self.imager.wait_for_image_saved()
        Filer().move_ram_to_shared(image_path)
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    def endpoint_spiral_next_step(self):
        """
        Takes the next step in the currently defined spiral path
        """
        assert self.mount is not None
        assert self.imager is not None

        logger.info("calling mount_spiral_offset_next() ...")
        self.mount.pw.mount_spiral_offset_next()
        self.mount.wait_until_settled(SettleMode.OFFSET_STEP)
        logger.info("mount stopped moving")

        if self.spirals_folder is not None:
            image_path = str(Path(self.spirals_folder) / Path("step-" + PathMaker().make_seq(self.spirals_folder) + ".fits"))
            self.imager.latest_settings = ImagerSettings(seconds=5, save=True, image_path=image_path, binning=1)
            self.imager.start_exposure(self.imager.latest_settings)
            self.imager.wait_for_image_saved()
            Filer().move_ram_to_shared(image_path)

        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    def endpoint_spiral_previous_step(self):
        """
        Goes back one step in the currently defined spiral path
        """
        assert self.mount is not None
        assert self.imager is not None

        logger.info("calling mount_spiral_offset_previous() ...")
        self.mount.pw.mount_spiral_offset_previous()
        self.mount.wait_until_settled(SettleMode.OFFSET_STEP)
        logger.info("mount stopped moving")

        if self.spirals_folder is not None:
            image_path = str(Path(self.spirals_folder) / Path("step-" + PathMaker().make_seq(self.spirals_folder) + ".fits"))
            self.imager.latest_settings = ImagerSettings(seconds=5, save=True, image_path=image_path, binning=1)
            self.imager.start_exposure(self.imager.latest_settings)
            self.imager.wait_for_image_saved()
            Filer().move_ram_to_shared(image_path)

        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    def endpoint_spiral_end_path(self):
        """
        Ends the currently defined spiral path
        """
        assert self.spiral_exposure_series is not None and self.imager is not None, "No spiral exposure series defined"
        self.imager.end_exposure_series(self.spiral_exposure_series)
        return CanonicalResponse_Ok

    @property
    def api_router(self) -> APIRouter:
        """
        Returns the FastApi router for the unit
        """

        router = APIRouter()

        base_path = Const.BASE_UNIT_PATH
        tag = "Unit"

        add_api_route(router, base_path + "/startup", tags=[tag], endpoint=self.endpoint_startup, methods=["PUT"])
        add_api_route(router, base_path + "/shutdown", tags=[tag], endpoint=self.endpoint_shutdown, methods=["PUT"])
        # The one state-changing verb kept on GET as well as PUT, deliberately and
        # temporarily. MAST_common's shared plan client aborts every committed unit with
        # method="GET" (models/plans.py:830-831), so PUT-only would answer 405 on the
        # fleet's abort path -- the last verb that should fail quietly. Accepting both is
        # the migration step: the client moves to PUT, then GET comes off here. Tracked
        # on #48; every other state-changing route in this file is PUT-only.
        add_api_route(router, base_path + "/abort", tags=[tag], endpoint=self.endpoint_abort, methods=["GET", "PUT"])
        add_api_route(router, base_path + "/status", tags=[tag], endpoint=self.endpoint_status)
        if self.autofocuser:
            add_api_route(
                router,
                base_path + "/start_autofocus",
                tags=[tag],
                endpoint=self.autofocuser.start_autofocus,
                methods=["PUT"],
            )
            add_api_route(
                router,
                base_path + "/stop_autofocus",
                tags=[tag],
                endpoint=self.autofocuser.endpoint_stop_autofocus,
                methods=["PUT"],
            )
        if self.acquirer:
            add_api_route(
                router,
                base_path + "/start_acquisition_and_guiding",
                tags=[tag],
                endpoint=self.acquirer.endpoint_start_acquisition_and_guiding,
                methods=["PUT"],
            )
        if self.guider:
            add_api_route(
                router,
                base_path + "/start_guiding",
                tags=[tag],
                endpoint=self.guider.endpoint_start_guiding,
                methods=["PUT"],
            )
            add_api_route(
                router,
                base_path + "/stop_acquisition_and_guiding",
                tags=[tag],
                endpoint=self.guider.endpoint_stop_acquisition_and_guiding,
                methods=["PUT"],
            )
        add_api_route(router, base_path + "/expose", tags=[tag], endpoint=self.expose, methods=["PUT"])
        add_api_route(
            router,
            base_path + "/test_stage_repeatability",
            tags=[tag],
            endpoint=self.endpoint_test_stage_repeatability,
            methods=["PUT"],
        )
        add_api_route(
            router,
            base_path + "/execute_assignment",
            methods=["PUT"],
            tags=[tag],
            endpoint=self.endpoint_execute_assignment,
        )
        add_api_route(
            router,
            base_path + "/start_dancing",
            tags=[tag],
            endpoint=self.endpoint_start_dancing,
            methods=["PUT"],
        )
        add_api_route(
            router,
            base_path + "/stop_dancing",
            tags=[tag],
            endpoint=self.endpoint_stop_dancing,
            methods=["PUT"],
        )
        # add_api_route(router,
        #     base_path + "/calculate_sky_pixel",
        #     tags=[tag],
        #     endpoint=self.set_sky_and_spec_pixel_values,
        # , methods=["PUT"])

        tag = "PlaneWave mount - spiral path"
        add_api_route(
            router, base_path + "/spiral_new_path", tags=[tag], endpoint=self.endpoint_spiral_new_path, methods=["PUT"]
        )
        add_api_route(
            router, base_path + "/spiral_next_step", tags=[tag], endpoint=self.endpoint_spiral_next_step, methods=["PUT"]
        )
        add_api_route(
            router,
            base_path + "/spiral_previous_step",
            tags=[tag],
            endpoint=self.endpoint_spiral_previous_step,
            methods=["PUT"],
        )
        add_api_route(
            router, base_path + "/spiral_end_path", tags=[tag], endpoint=self.endpoint_spiral_end_path, methods=["PUT"]
        )

        return router


def serialize_ip_addresses(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: serialize_ip_addresses(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [serialize_ip_addresses(item) for item in data]
    elif isinstance(data, ipaddress.IPv4Address):
        return str(data)
    else:
        return data
