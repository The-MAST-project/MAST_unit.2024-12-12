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
from typing import Annotated, Any, Literal

import numpy as np
from fastapi import Query
from fastapi.routing import APIRouter
from PIL import Image

# from pydantic import Field
from starlette.websockets import WebSocket, WebSocketDisconnect

from acquirer import Acquirer
from acquisition import Acquisition
from autofocusing import Autofocuser, AutofocusResult
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
from common.const import Const
from common.corrections import correction_phases
from common.dlipowerswitch import (
    PowerStatus,
    PowerSwitchFactory,
    PowerSwitchStatus,
    SwitchedOutlet,
)
from common.filer import Filer
from common.interfaces.components import Component, ComponentStatus
from common.interfaces.guiding import GuiderTypes

# from guiding import Guider
from common.interfaces.imager import (
    ImagerBinning,
    ImagerExposureSeries,
    ImagerSettings,
    ImagerStatus,
    ImagerTypes,
)
from common.mast_logging import DailyFileHandler, init_log
from common.models.assignments import UnitAssignmentModel
from common.parsers import sexagesimal_degrees_to_decimal, sexagesimal_hours_to_decimal
from common.paths import PathMaker
from common.rois import UnitRoi
from common.tasks.notifications import notify_controller_about_task_acquisition_path
from common.utils import RepeatTimer, function_name, time_stamp
from covers import Covers, CoverStatus
from focuser import Focuser, FocuserStatus
from imagers import Imager
from mount import Mount, MountStatus
from phd2.phd2 import PHD2Connector, PHD2GuiderStatus, PHD2ImagerStatus
from PlaneWave import pwi4_client
from solving import Solver
from stage import Stage, StageStatus

RA_REGEX = r"^(\d{1,2}):(\d{2}):(\d{2}(?:\.\d{1,3})?)$"
DEC_REGEX = r"^([+-]?)(\d{1,2}):(\d{2}):(\d{2}(?:\.\d{1,3})?)$"

logger = logging.getLogger("mast.unit")
init_log(logger)
filer = Filer(logger)


def configured_imager() -> ImagerTypes:
    t = Config().get_unit().imager.imager_type
    if t.startswith("ascom"):
        t = "ascom"
    return ImagerTypes(t)


def get_imager_type(
    imager_type: ImagerTypes = Query(default=configured_imager()),
) -> ImagerTypes:
    return imager_type or configured_imager()


class GuideDirections(Enum):
    guide_north = 0
    guide_south = 1
    guide_east = 2
    guide_west = 3


class UnitStatus(ComponentStatus, PowerStatus):
    id: int
    guiding: bool = False
    autofocusing: bool = False
    power_switch: PowerSwitchStatus | None = None
    mount: MountStatus | None = None
    imager: ImagerStatus | PHD2ImagerStatus | None = None
    covers: CoverStatus | None = None
    focuser: FocuserStatus | None = None
    stage: StageStatus | None = None
    guider: PHD2GuiderStatus | None = None
    errors: list[str] | None = None
    autofocus: dict | None = None
    corrections: list | None = None
    type: Literal["short", "full"] = "full"
    date: str | None = None
    powered: bool = True


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

    def __init__(self, id_: int | str):
        from guiding import Guider

        if self._initialized:
            return
        # logger.info(f"Unit.__init__: initiating instance 0x{id(self):x}")

        Component.__init__(self)

        self._connected: bool = False

        self.was_tracking_before_guiding: bool = False

        file_handler = [h for h in logger.handlers if isinstance(h, DailyFileHandler)]
        logger.info(f"logging to '{file_handler[0].path}'")

        if isinstance(id_, int):
            if id_ > Unit.MAX_UNITS:
                raise Exception(
                    f"Bad unit id '{id_}', must be in [1..{Unit.MAX_UNITS}]"
                )
            else:
                id_ = int(id_)

        self.id = id_
        self.unit_conf = Config().get_unit()

        self.min_ra_correction_arcsec = self.unit_conf.guiding.min_ra_correction_arcsec
        self.min_dec_correction_arcsec = (
            self.unit_conf.guiding.min_dec_correction_arcsec
        )

        self.autofocus_max_tolerance = self.unit_conf.autofocus.max_tolerance
        self.autofocus_try: int = 0

        self.hostname = socket.gethostname()
        try:
            self.power_switch = PowerSwitchFactory.get_instance()
            self.mount: Mount = Mount(self)
            self.imager: Imager = Imager(self)
            self.covers: Covers = Covers(self)
            self.stage: Stage = Stage(self)
            self.focuser: Focuser = Focuser(self)
            self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()

            self.autofocuser: Autofocuser = Autofocuser(self)
            self.solver: Solver = Solver(self)
            self.acquirer: Acquirer = Acquirer(self)
            self.guider: Guider = Guider(self)
        except Exception as ex:
            logger.exception(msg="could not create a Unit", exc_info=ex)
            raise ex

        self.components: list[Component] = [
            self.power_switch,
            self.mount,
            self.imager,
            self.covers,
            self.focuser,
            self.stage,
        ]

        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = "unit-timer-thread"
        self.timer.start()

        self.reference_image: np.ndarray | None = None
        self.autofocus_result: AutofocusResult | None = None

        self._was_shut_down = False

        self.connected_clients: list[WebSocket] = []
        # self.imager.register_visualizer('image-to-dashboard', self.push_image_to_dashboards)

        self.errors: list[str] = []

        self.controller_api = ControllerApi()

        self.spirals_folder: str | None = None
        self.spiral_exposure_series: ImagerExposureSeries | None = None
        self.latest_acquisition: Acquisition | None = None

        self._initialized = True
        logger.info("unit: initialized")

    def do_startup(self):
        self.start_activity(UnitActivities.StartingUp)
        [comp.startup() for comp in self.components]

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
        self._was_shut_down = True
        self.timer.cancel()
        self.unit_shutdown_event.set()

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
        return all([comp.connected for comp in self.components])

    @connected.setter
    def connected(self, value: bool):
        """
        Should connect/disconnect anything that needs connecting/disconnecting
        """
        self.mount.connected = value
        self.imager.connected = value
        self.covers.connected = value
        self.stage.connected = value
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
            if isinstance(c, SwitchedOutlet):
                c.power_off()

    def status(self) -> CanonicalResponse:
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

        if self.acquirer.latest_acquisition:
            corrections_list = (
                self.acquirer.latest_acquisition.corrections
                if (
                    self.acquirer.latest_acquisition
                    and self.acquirer.latest_acquisition.corrections
                )
                else []
            )

            for phase in correction_phases:
                if phase in corrections_list and isinstance(corrections_list, dict):
                    correction = corrections_list[phase]
                    if isinstance(correction, list):
                        all_corrections.extend(correction)
                    else:
                        all_corrections.append(correction)

        assert self.imager is not None, "Imager must be initialized"
        assert self.guider is not None, "Guider must be initialized"
        imager_status = (
            PHD2Connector(parent_imager=self.imager).status(capacity="imager")
            if self.imager.backend_type == ImagerTypes.Phd2
            else self.imager.status()
        )
        guider_status = (
            PHD2Connector(parent_imager=self.imager).status(capacity="guider")
            if self.guider.guider_type == GuiderTypes.Phd2
            else self.guider.status()
        )
        ret = UnitStatus(
            **self.component_status().model_dump(),
            id=id(self),
            guiding=self.guider.is_guiding,
            autofocusing=self.autofocuser.is_autofocusing,
            power_switch=self.power_switch.status(),
            mount=self.mount.status(),
            imager=imager_status,  # type: ignore
            covers=self.covers.status(),
            focuser=self.focuser.status(),
            stage=self.stage.status(),
            guider=guider_status,  # type: ignore
            # solver= self.solver.status(),
            errors=self.errors,
            autofocus=autofocus,
            corrections=all_corrections,
            type="full",
            date=time_stamp(),
        ).model_dump()

        return CanonicalResponse(value=serialize_ip_addresses(ret))

    @staticmethod
    def quit():
        """
        Quits the application
        """
        from app import app_quit

        app_quit(reason="quit()")

    def abort(self):
        """
        Aborts any in-progress mount activity
        """
        if self.is_active(UnitActivities.Guiding):
            self.guider.stop_acquisition_and_guiding()
            while self.is_active(UnitActivities.Guiding):
                time.sleep(0.2)

        if self.is_active(UnitActivities.AutofocusingPWI4) or self.is_active(
            UnitActivities.Autofocusing
        ):
            self.autofocuser.stop_autofocus()
            while self.is_active(UnitActivities.AutofocusingPWI4) or self.is_active(
                UnitActivities.Autofocusing
            ):
                time.sleep(0.2)

        [component.abort() for component in self.components]

    def ontimer(self):  # noqa: C901
        if self.unit_shutdown_event.is_set():
            self.timer.cancel()
            return

        """
        Used in order to end activities that were started elsewhere in the code.
        """
        # UnitActivities.StartingUp
        if self.is_active(UnitActivities.StartingUp) and not (
            self.mount.is_active(MountActivities.StartingUp)
            or self.imager.is_active(ImagerActivities.StartingUp)
            or self.stage.is_active(StageActivities.StartingUp)
            or self.focuser.is_active(FocuserActivities.StartingUp)
            or self.covers.is_active(CoverActivities.StartingUp)
        ):
            self.end_activity(UnitActivities.StartingUp)

        # UnitActivities.ShuttingDown
        if self.is_active(UnitActivities.ShuttingDown) and not (
            self.mount.is_active(MountActivities.ShuttingDown)
            or self.imager.is_active(ImagerActivities.ShuttingDown)
            or self.stage.is_active(StageActivities.ShuttingDown)
            or self.focuser.is_active(FocuserActivities.ShuttingDown)
            or self.covers.is_active(CoverActivities.ShuttingDown)
            or self.mount.is_active(MountActivities.ShuttingDown)
        ):
            self.end_activity(UnitActivities.ShuttingDown)
            self._was_shut_down = True

        # UnitActivities.AutofocusingPWI4
        if self.is_active(UnitActivities.AutofocusingPWI4):
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
                    self.unit_conf.focuser.known_as_good_position = best_position
                    try:
                        Config().set_unit(self.hostname, self.unit_conf)
                        logger.info(
                            f"autofocus: saved {best_position=} in the configuration for unit {self.hostname}."
                        )
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
                self.autofocus_result.time_stamp = datetime.datetime.now().isoformat()

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
        return all([c.operational for c in self.components])

    @property
    def why_not_operational(self) -> list[str]:
        return list(chain.from_iterable(c.why_not_operational for c in self.components))

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
            except Exception as e:
                logger.error(f"websocket.send error: {e}")

    def expose(
        self,
        ra_j2000_hours: Annotated[
            str | float | None,
            Query(
                regex=RA_REGEX + r"|^\d{1,2}(\.\d+)?$",
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
                regex=DEC_REGEX + r"|^[-+]?\d{1,2}(\.\d+)?$",
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
        binning: int = 1,
        gain: int = 170,
        ra_offsets: Annotated[
            str | list[str] | list[float] | None,
            Query(
                description=(
                    "#### Optional list of RA offsets (arcsec) between exposures:\n"
                    "- empty - no RA offsetting\n"
                    "- list of floats - MUST be same length as `repeats`"
                ),
            ),
        ] = None,
        dec_offsets: Annotated[
            str | list[str] | list[float] | None,
            Query(
                description=(
                    "#### Optional list of DEC offsets (arcsec) between exposures:\n"
                    "- empty - no DEC offsetting\n"
                    "- list of floats - MUST be same length as `repeats`"
                ),
            ),
        ] = None,
    ) -> CanonicalResponse:

        if ra_j2000_hours:
            if isinstance(ra_j2000_hours, str):
                if ":" in ra_j2000_hours:
                    ra_j2000_hours = sexagesimal_hours_to_decimal(ra_j2000_hours)
                else:
                    ra_j2000_hours = float(ra_j2000_hours)
            elif isinstance(ra_j2000_hours, float):
                pass

        if dec_j2000_degs:
            if isinstance(dec_j2000_degs, str):
                if ":" in dec_j2000_degs:
                    dec_j2000_degs = sexagesimal_degrees_to_decimal(dec_j2000_degs)
                else:
                    dec_j2000_degs = float(dec_j2000_degs)
            elif isinstance(dec_j2000_degs, float):
                pass

        if (ra_j2000_hours is not None and isinstance(ra_j2000_hours, float)) and (
            dec_j2000_degs is not None and isinstance(dec_j2000_degs, float)
        ):
            logger.info(f"slewing mount to ra={ra_j2000_hours}, dec={dec_j2000_degs}")
            self.mount.goto_ra_dec_j2000(ra=ra_j2000_hours, dec=dec_j2000_degs)
            while self.mount.is_moving:
                logger.info("waiting for mount to stop moving ...")
                time.sleep(1)

        if ra_offsets is not None:
            if isinstance(ra_offsets, str):
                ra_offsets = ra_offsets.split()
            if (
                len(ra_offsets) != 1 and len(ra_offsets) != repeats
            ):  # one element or the same number of elements as repeats
                return CanonicalResponse(
                    errors=[f"ra_offsets must have {repeats} elements"]
                )
            ra_offsets = (
                [float(ra_offsets[0])] * repeats
                if len(ra_offsets) == 1
                else [float(val) for val in ra_offsets]
            )

        if dec_offsets is not None:
            if isinstance(dec_offsets, str):
                dec_offsets = dec_offsets.split()
            if (
                len(dec_offsets) != 1 and len(dec_offsets) != repeats
            ):  # one element or the same number of elements as repeats
                return CanonicalResponse(
                    errors=[f"dec_offsets must have {repeats} elements"]
                )
            dec_offsets = (
                [float(dec_offsets[0])] * repeats
                if len(dec_offsets) == 1
                else [float(val) for val in dec_offsets]
            )

        if fiber_x is None and fiber_y is None and width is None and height is None:
            width = self.imager.camera_x_size
            height = self.imager.camera_y_size
            if not width or not height:
                return CanonicalResponse(
                    errors=["cannot get width and height from the imager"]
                )
            fiber_x = int(width / 2)
            fiber_y = int(height / 2)

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

    def do_expose(  # noqa: C901
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
        binning: int = 1,
        gain: int = 170,
    ) -> CanonicalResponse:

        op = function_name()
        seconds = exposure_seconds

        if binning not in [1, 2, 4]:
            return CanonicalResponse(errors=[f"bad {binning=}, should be 1, 2 or 4"])

        self.mount.start_tracking()
        exposure_series = self.imager.start_exposure_series(purpose="unit.do_exposure")
        for repeat in range(repeats):
            end = None
            if seconds_between_exposures != 0.0:
                start = datetime.datetime.now()
                end = start + datetime.timedelta(seconds=seconds_between_exposures)

            unit_roi = UnitRoi(fiber_x, fiber_y, width, height)
            imager_binning: ImagerBinning = ImagerBinning(x=binning, y=binning)
            default_folder = PathMaker().make_exposures_folder()
            base_folder = (
                os.path.join(default_folder, subfolder) if subfolder else default_folder
            )
            imager_settings = ImagerSettings(
                seconds=seconds,
                base_folder=base_folder,
                gain=gain,
                binning=imager_binning,
                roi=unit_roi.to_imager_roi(binning=imager_binning),
                tags={"roi": None},
                save=True,
            )
            self.imager.latest_settings = imager_settings

            logger.info(f"{op}: starting exposure #{repeat} (of {repeats})")
            self.imager.start_exposure(imager_settings)

            if not (
                self.imager.latest_settings is None
                or self.imager.latest_settings.image_path is None
            ):
                self.imager.wait_for_image_saved()
                filer.move_ram_to_shared(self.imager.latest_settings.image_path)

            if end is not None and seconds_between_exposures != 0.0:
                now = datetime.datetime.now()
                if now < end:
                    period = (end - now).seconds
                    logger.info(
                        f"{op}: sleeping {period} seconds till next exposure ..."
                    )
                    time.sleep(period)

            if ra_offsets is not None or dec_offsets is not None:
                if ra_offsets is not None and dec_offsets is not None:
                    logger.info(
                        f"offsetting mount ra={ra_offsets[repeat]}, dec={dec_offsets[repeat]}"
                    )
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
                while self.mount.is_moving:
                    logger.info("waiting for mount to stop moving ...")
                    time.sleep(1)

        self.imager.end_exposure_series(exposure_series)
        self.mount.stop_tracking()
        return CanonicalResponse_Ok

    def test_stage_repeatability(
        self,
        start_position: int | str = 50000,
        end_position: int | str = 300000,
        step: int | str = 25000,
        exposure_seconds: int | str = 5,
        binning: int | str = 1,
        gain: int | str = 170,
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
        binning: int | str = 1,
        gain: int | str = 170,
    ) -> CanonicalResponse:
        op = function_name()

        if isinstance(start_position, str):
            start_position = int(start_position)
        if isinstance(end_position, str):
            end_position = int(end_position)
        if isinstance(step, str):
            step = int(step)
        if isinstance(exposure_seconds, str):
            exposure_seconds = int(exposure_seconds)
        if isinstance(binning, str):
            binning = int(binning)
        if isinstance(gain, str):
            gain = int(gain)

        reference_position = start_position

        repeatablility_exposure_series = self.imager.start_exposure_series(
            "unit.do_test_stage_repeatability"
        )

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
                binning=ImagerBinning(x=binning, y=binning),
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
                binning=ImagerBinning(x=binning, y=binning),
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

    def do_execute_assignment(self, assignment: UnitAssignmentModel):
        """
        Execute an assignment in a separate Thread
        :param assignment:
        :return:
        """
        if assignment.task.autofocus:
            self.autofocuser.start_autofocus(
                ra_j2000_hours=assignment.target.ra,
                dec_j2000_degs=assignment.target.dec,
            )

            while self.is_active(UnitActivities.Autofocusing):
                time.sleep(10)

            #
            # If UnitActivities.Autofocusing ends in success, an autofocuser result should
            #  be available
            #
            if not self.autofocuser.latest_result:
                return  # should propagate errors as well

            if (
                assignment.task.ulid is not None
                and self.imager.latest_settings
                and self.imager.latest_settings.image_path
            ):
                notify_controller_about_task_acquisition_path(
                    task_id=assignment.task.ulid,
                    link="autofocus",
                    src=Path(self.imager.latest_settings.image_path).parent.name,
                )

            #
            # At this point we have autofocused and can start acquisition
            #
            self.acquirer.start_acquisition_and_guiding(
                ra_j2000_hours=assignment.target.ra,
                dec_j2000_degs=assignment.target.dec,
            )

            if (
                assignment.task.ulid is not None
                and self.acquirer.latest_acquisition is not None
            ):
                notify_controller_about_task_acquisition_path(
                    task_id=assignment.task.ulid,
                    link="acquisition",
                    src=self.acquirer.latest_acquisition.folder,
                )

    async def execute_assignment(self, assignment: UnitAssignmentModel):
        if not self.operational:
            return CanonicalResponse(errors=self.why_not_operational)

        if not self.is_idle():
            return CanonicalResponse(errors=[f"busy ({self.activities=})"])

        Thread(target=self.do_execute_assignment, args=[assignment]).start()

        return CanonicalResponse_Ok

    @staticmethod
    async def set_sky_and_spec_pixel_values(
        sky_x: int, sky_y: int, spec_x: int, spec_y: int
    ):

        cfg = Config().get_unit()

        cfg.acquisition.roi.sky_x = sky_x
        cfg.acquisition.roi.sky_y = sky_y
        cfg.guiding.roi.fiber_x = spec_x
        cfg.guiding.roi.fiber_y = spec_y

        Config().set_unit(unit_name=cfg.name, unit_conf=cfg)

        return CanonicalResponse_Ok

    def spiral_new_path(self, x_step_arcsec: float, y_step_arcsec: float):
        """
        Defines a new spiral path<br>
        **NOTE**: Remember to call `spiral_end_path()` when done with the spiral path
        """
        self.mount.pw.mount_spiral_offset_new(
            x_step_arcsec=x_step_arcsec, y_step_arcsec=y_step_arcsec
        )
        self.spirals_folder = PathMaker().make_spirals_folder()

        image_path = os.path.join(
            self.spirals_folder,
            "step-" + PathMaker().make_seq(self.spirals_folder) + ".fits",
        )
        self.imager.latest_settings = ImagerSettings(
            seconds=5, save=True, image_path=image_path
        )
        self.spiral_exposure_series = self.imager.start_exposure_series(
            purpose="spiral_new_path"
        )
        self.imager.start_exposure(self.imager.latest_settings)
        self.imager.wait_for_image_saved()
        Filer().move_ram_to_shared(image_path)
        return CanonicalResponse_Ok

    def spiral_next_step(self):
        """
        Takes the next step in the currently defined spiral path
        """
        logger.info("calling mount_spiral_offset_next() ...")
        self.mount.pw.mount_spiral_offset_next()
        while self.mount.is_moving:
            time.sleep(1)
        logger.info("mount stopped moving")

        if self.spirals_folder is not None:
            image_path = str(
                Path(self.spirals_folder)
                / Path("step-" + PathMaker().make_seq(self.spirals_folder) + ".fits")
            )
            self.imager.latest_settings = ImagerSettings(
                seconds=5, save=True, image_path=image_path
            )
            self.imager.start_exposure(self.imager.latest_settings)
            self.imager.wait_for_image_saved()
            Filer().move_ram_to_shared(image_path)

        return CanonicalResponse_Ok

    def spiral_previous_step(self):
        """
        Goes back one step in the currently defined spiral path
        """
        logger.info("calling mount_spiral_offset_previous() ...")
        self.mount.pw.mount_spiral_offset_previous()
        while self.mount.is_moving:
            time.sleep(1)
        logger.info("mount stopped moving")

        if self.spirals_folder is not None:
            image_path = str(
                Path(self.spirals_folder)
                / Path("step-" + PathMaker().make_seq(self.spirals_folder) + ".fits")
            )
            self.imager.latest_settings = ImagerSettings(
                seconds=5, save=True, image_path=image_path
            )
            self.imager.start_exposure(self.imager.latest_settings)
            self.imager.wait_for_image_saved()
            Filer().move_ram_to_shared(image_path)

        return CanonicalResponse_Ok

    def spiral_end_path(self):
        """
        Ends the currently defined spiral path
        """
        assert (
            self.spiral_exposure_series is not None
        ), "No spiral exposure series defined"
        self.imager.end_exposure_series(self.spiral_exposure_series)
        return CanonicalResponse_Ok

    @property
    def api_router(self) -> APIRouter:
        """
        Returns the FastApi router for the unit
        """

        router = APIRouter()

        tag = "unit"
        router.add_api_route(base_path + "/startup", tags=[tag], endpoint=self.startup)
        router.add_api_route(
            base_path + "/shutdown", tags=[tag], endpoint=self.shutdown
        )
        router.add_api_route(base_path + "/abort", tags=[tag], endpoint=self.abort)
        router.add_api_route(base_path + "/status", tags=[tag], endpoint=self.status)
        router.add_api_route(
            base_path + "/start_autofocus",
            tags=[tag],
            endpoint=self.autofocuser.start_autofocus,
        )
        router.add_api_route(
            base_path + "/stop_autofocus",
            tags=[tag],
            endpoint=self.autofocuser.stop_autofocus,
        )
        router.add_api_route(
            base_path + "/stop_acquisition_and_guiding",
            tags=[tag],
            endpoint=self.guider.stop_acquisition_and_guiding,
        )
        router.add_api_route(
            base_path + "/start_acquisition_and_guiding",
            tags=[tag],
            endpoint=self.acquirer.start_acquisition_and_guiding,
        )
        router.add_api_route(base_path + "/expose", tags=[tag], endpoint=self.expose)
        router.add_api_route(
            base_path + "/test_stage_repeatability",
            tags=[tag],
            endpoint=self.test_stage_repeatability,
        )
        router.add_api_route(
            base_path + "/execute_assignment",
            methods=["PUT"],
            tags=[tag],
            endpoint=self.execute_assignment,
        )
        router.add_api_route(
            base_path + "/calculate_sky_pixel",
            tags=[tag],
            endpoint=self.set_sky_and_spec_pixel_values,
        )

        tag = "PlaneWave mount - spiral path"
        router.add_api_route(
            base_path + "/spiral_new_path", tags=[tag], endpoint=self.spiral_new_path
        )
        router.add_api_route(
            base_path + "/spiral_next_step", tags=[tag], endpoint=self.spiral_next_step
        )
        router.add_api_route(
            base_path + "/spiral_previous_step",
            tags=[tag],
            endpoint=self.spiral_previous_step,
        )
        router.add_api_route(
            base_path + "/spiral_end_path", tags=[tag], endpoint=self.spiral_end_path
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


# unit_id: int | str | None = None
hostname = socket.gethostname()
if hostname.startswith("mast"):
    try:
        unit_id = int(hostname[4:])
    except ValueError:
        unit_id = hostname[4:]
else:
    logger.error(f"Cannot figure out the MAST unit_id ({hostname=})")

base_path = Const.BASE_UNIT_PATH
tag = "Unit"

unit = None
if not unit:
    unit = Unit(id_=unit_id)
