import datetime
import io
import os
from itertools import chain
import logging
import socket
import numpy as np
import camera
from PlaneWave import pwi4_client
import time
from typing import List, Any, Optional, Union
from camera import Camera, CameraBinning
from covers import Covers
from stage import Stage
from mount import Mount
from focuser import Focuser
from common.dlipowerswitch import PowerSwitchFactory, SwitchedOutlet
from common.utils import RepeatTimer
from threading import Thread
from common.utils import Component, BASE_UNIT_PATH, UnitRoi
from common.mast_logging import DailyFileHandler, init_log
from common.utils import time_stamp, CanonicalResponse, CanonicalResponse_Ok, function_name, OperatingMode
from common.filer import Filer
from common.config import Config
from common.activities import UnitActivities, FocuserActivities, CameraActivities
from common.activities import CoverActivities, StageActivities, MountActivities
from common.corrections import correction_phases
from common.paths import PathMaker
from enum import Enum
from fastapi.routing import APIRouter
from PIL import Image
import ipaddress
from starlette.websockets import WebSocket, WebSocketDisconnect
from common.models.assignments import UnitAssignmentModel
from common.api import ControlerApi
from common.tasks.models import TaskProduct

from autofocusing import Autofocuser, AutofocusResult
from solving import Solver
from acquirer import Acquirer
from guiding import Guider

logger = logging.getLogger('mast.unit')
init_log(logger)
filer = Filer(logger)


class GuideDirections(Enum):
    guideNorth = 0
    guideSouth = 1
    guideEast = 2
    guideWest = 3


class Unit(Component):

    MAX_UNITS = 20
    MAX_AUTOFOCUS_TRIES = 3

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Unit, cls).__new__(cls)
            # logger.info(f"Unit.__new__: allocated instance 0x{id(cls._instance):x}")
        return cls._instance

    def __init__(self, id_: Union[int, str]):
        if self._initialized:
            return
        logger.info(f"Unit.__init__: initiating instance 0x{id(self):x}")

        Component.__init__(self)

        self.operating_mode = OperatingMode.Night
        if 'UNIT_OPERATING_MODE' in os.environ:
            self.operating_mode = OperatingMode.Day if os.environ['UNIT_OPERATING_MODE'].lower() == 'day' \
                else OperatingMode.Night

        self._connected: bool = False

        self.was_tracking_before_guiding: bool = False

        file_handler = [h for h in logger.handlers if isinstance(h, DailyFileHandler)]
        logger.info(f"logging to '{file_handler[0].path}'")

        if isinstance(id_, int):
            if id_ > Unit.MAX_UNITS:
                raise f"Bad unit id '{id_}', must be in [1..{Unit.MAX_UNITS}]"
            else:
                id_ = int(id_)

        self.id = id_
        self.unit_conf = Config().get_unit()

        self.min_ra_correction_arcsec: float = float(self.unit_conf['guiding']['min_ra_correction_arcsec']) \
            if 'min_ra_correction_arcsec' in self.unit_conf['guiding'] else 1
        self.min_dec_correction_arcsec: float = float(self.unit_conf['guiding']['min_dec_correction_arcsec']) \
            if 'min_dec_correction_arcsec' in self.unit_conf['guiding'] else 1

        self.autofocus_max_tolerance = self.unit_conf['autofocus']['max_tolerance']
        self.autofocus_try: int = 0

        self.operating_mode: OperatingMode = OperatingMode.Night

        self.hostname = socket.gethostname()
        try:
            # self.power_switch = PowerSwitchFactory.get_instance(
            #     conf=self.unit_conf['power_switch'],
            #     upload_outlet_names=True)
            self.power_switch = PowerSwitchFactory.get_instance()
            self.mount: Mount = Mount(self)
            self.camera: Camera = Camera(self)
            self.covers: Covers = Covers(self)
            self.stage: Stage = Stage(self)
            self.focuser: Focuser = Focuser(self)
            self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()

            self.autofocuser: Autofocuser = Autofocuser(self)
            self.solver: Solver = Solver(self)
            self.acquirer: Acquirer = Acquirer(self)
            self.guider: Guider = Guider(self)
        except Exception as ex:
            logger.exception(msg='could not create a Unit', exc_info=ex)
            raise ex

        self.components: List[Component] = [
            self.power_switch,
            self.mount,
            self.camera,
            self.covers,
            self.focuser,
            self.stage,
        ]

        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'unit-timer-thread'
        self.timer.start()

        self.reference_image = None
        self.autofocus_result: Optional[AutofocusResult] = None

        self._was_shut_down = False

        self.connected_clients: List[WebSocket] = []
        # self.camera.register_visualizer('image-to-dashboard', self.push_image_to_dashboards)

        self.errors: List[str] = []

        self.controller_api = ControlerApi()

        self._initialized = True
        logger.info("unit: initialized")

    def do_startup(self):
        self.start_activity(UnitActivities.StartingUp)
        [comp.startup() for comp in self.components]

    def startup(self):
        """
        Starts the **MAST** ``unit`` subsystem.  Makes it ``operational``.

        Returns
        -------

        :mastapi:
        """
        if self.is_active(UnitActivities.StartingUp):
            return

        self._was_shut_down = False
        Thread(name='unit-startup-thread', target=self.do_startup).start()
        return CanonicalResponse_Ok

    def do_shutdown(self):
        self.start_activity(UnitActivities.ShuttingDown)
        [comp.shutdown() for comp in self.components]
        self._was_shut_down = True

    def shutdown(self):
        """
        Shuts down the **MAST** ``unit`` subsystem.  Makes it ``idle``.

        :mastapi:
        """
        if not self.connected:
            self.connect()

        if self.is_active(UnitActivities.ShuttingDown):
            return

        Thread(name='shutdown-thread', target=self.do_shutdown).start()
        return CanonicalResponse_Ok

    @property
    def connected(self):
        return all([comp.connected for comp in self.components])

    @connected.setter
    def connected(self, value):
        """
        Should connect/disconnect anything that needs connecting/disconnecting

        """
        self.mount.connected = value
        self.camera.connected = value
        self.covers.connected = value
        self.stage.connected = value
        self.focuser.connected = value

    def connect(self):
        """
        Connects the **MAST** ``unit`` subsystems to all its ancillaries.

        :mastapi:
        """
        self.connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        Disconnects the **MAST** ``unit`` subsystems from all its ancillaries.

        :mastapi:
        """
        self.connected = False
        return CanonicalResponse_Ok

    def power_all_on(self):
        """
        Turn **ON** all power sockets

        :mastapi:
        """
        for c in self.components:
            if isinstance(c, SwitchedOutlet):
                c.power_on()

    def power_all_off(self):
        """
        Turn **OFF** all power sockets

        :mastapi:
        """
        for c in self.components:
            if isinstance(c, SwitchedOutlet):
                c.power_off()

    def status(self) -> CanonicalResponse:
        """
        Returns
        -------
        UnitStatus
        :mastapi:
        """
        ret = self.component_status()
        ret |= {
            'id': id(self),
            'guiding': self.guider.is_guiding,
            'autofocusing': self.autofocuser.is_autofocusing,
        }
        for comp in self.components:
            ret[comp.name] = comp.status()
        time_stamp(ret)

        if self.autofocus_result:
            ret['autofocus'] = {
                'success': self.autofocus_result.success,
                'best_position': self.autofocus_result.best_position,
                'tolerance': self.autofocus_result.tolerance,
                'time_stamp': self.autofocus_result.time_stamp
            }

        # if (self.corrections and 'target' in self.corrections and 'ra' in self.corrections['target']
        #         and 'sequence' in self.corrections):
        #     ret['corrections'] = {
        #         'target': {
        #             'ra': self.corrections['target']['ra'],
        #             'dec': self.corrections['target']['dec'],
        #         },
        #         'sequence': self.corrections['sequence'],
        #     }
        if self.acquirer.latest_acquisition and self.acquirer.latest_acquisition.corrections:
            corrections = self.acquirer.latest_acquisition.corrections
            ret['corrections'] = [corrections[phase].to_dict() for phase in correction_phases if phase in corrections]

        if self.errors:
            ret['errors'] = self.errors

        ret['powered'] = True
        ret['type'] = 'full'

        return CanonicalResponse(value=serialize_ip_addresses(ret))

    @staticmethod
    def quit():
        """
        Quits the application

        :mastapi:
        Returns
        -------

        """
        from app import app_quit
        app_quit()

    def abort(self):
        """
        Aborts any in-progress mount activity

        :mastapi:
        Returns
        -------

        """
        if self.is_active(UnitActivities.Guiding):
            self.guider.stop_acquisition_and_guiding()
            while self.is_active(UnitActivities.Guiding):
                time.sleep(.2)

        if self.is_active(UnitActivities.AutofocusingPWI4) or self.is_active(UnitActivities.Autofocusing):
            self.autofocuser.stop_autofocus()
            while self.is_active(UnitActivities.AutofocusingPWI4) or self.is_active(UnitActivities.Autofocusing):
                time.sleep(.2)

        [component.abort() for component in self.components]

    def ontimer(self):
        """
        Used in order to end activities that were started elsewhere in the code.

        Returns
        -------

        """
        # UnitActivities.StartingUp
        if self.is_active(UnitActivities.StartingUp):
            if not (self.mount.is_active(MountActivities.StartingUp) or
                    self.camera.is_active(CameraActivities.StartingUp) or
                    self.stage.is_active(StageActivities.StartingUp) or
                    self.focuser.is_active(FocuserActivities.StartingUp) or
                    self.covers.is_active(CoverActivities.StartingUp)):
                self.end_activity(UnitActivities.StartingUp)

        # UnitActivities.ShuttingDown
        if self.is_active(UnitActivities.ShuttingDown):
            if not (self.mount.is_active(MountActivities.ShuttingDown) or
                    self.camera.is_active(CameraActivities.ShuttingDown) or
                    self.stage.is_active(StageActivities.ShuttingDown) or
                    self.focuser.is_active(FocuserActivities.ShuttingDown) or
                    self.covers.is_active(CoverActivities.ShuttingDown) or
                    self.mount.is_active(MountActivities.ShuttingDown)):
                self.end_activity(UnitActivities.ShuttingDown)
                self._was_shut_down = True

        # UnitActivities.AutofocusingPWI4
        if self.is_active(UnitActivities.AutofocusingPWI4):
            autofocus_status = self.pw.status().autofocus
            if not autofocus_status:
                logger.error('Empty PWI4 autofocus status')
            elif not autofocus_status.is_running:   # it's done
                logger.info('PWI4 autofocus ended, getting status.')
                self.autofocus_result = AutofocusResult()
                self.autofocus_result.success = autofocus_status.success
                if self.autofocus_result.success:
                    self.autofocus_result.best_position = autofocus_status.best_position
                    self.autofocus_result.tolerance = autofocus_status.tolerance

                    best_position = autofocus_status.best_position
                    self.unit_conf['focuser']['known_as_good_position'] = best_position
                    try:
                        Config().set_unit(self.hostname, self.unit_conf)
                        logger.info(f"autofocus: saved {best_position=} in the configuration for unit {self.hostname}.")
                        if autofocus_status.tolerance > self.autofocus_max_tolerance:
                            if self.autofocus_try < Unit.MAX_AUTOFOCUS_TRIES:
                                self.autofocus_try += 1
                                logger.info(f"autofocus: latest {autofocus_status.tolerance=} greater than" +
                                            f"{self.autofocus_max_tolerance=}, starting autofocus " +
                                            f"try #{self.autofocus_try}")
                                self.autofocuser.start_pwi4_autofocus()
                            else:
                                logger.info(f"autofocus: failed to reach {self.autofocus_max_tolerance=} " +
                                            f"in {Unit.MAX_AUTOFOCUS_TRIES=}")
                        else:
                            self.autofocus_try = 0

                    except Exception as e:
                        logger.exception("failed to save unit_conf for ['focuser']['know_as_good_position']",
                                         exc_info=e)
                else:
                    logger.error(f"PlaneWave autofocus failed")
                    self.autofocus_result.best_position = None
                    self.autofocus_result.tolerance = None
                self.autofocus_result.time_stamp = datetime.datetime.now().isoformat()

                self.end_activity(UnitActivities.AutofocusingPWI4)
            else:
                logger.info(f'PlaneWave autofocus in progress {self.autofocus_try=}')

    def end_lifespan(self):
        logger.info('unit end lifespan')
        self.shutdown()

    def start_lifespan(self):
        logger.debug('unit start lifespan')
        self.startup()

    @property
    def operational(self) -> bool:
        return all([c.operational for c in self.components])

    @property
    def why_not_operational(self) -> List[str]:
        return list(chain.from_iterable(c.why_not_operational for c in self.components))

    @property
    def name(self) -> str:
        return 'unit'

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
                await websocket.send(png_data)
                # loop = asyncio.get_event_loop()
                # loop.run_until_complete(websocket.send(png_data))
            except Exception as e:
                logger.error(f"websocket.send error: {e}")

    def expose_with_roi(self,
                        subfolder: Optional[str] = None,
                        exposure_seconds: Union[float, str] = 3,
                        repeats: Union[int, str] = 1,
                        seconds_between_exposures: Union[float, str] = 0,
                        fiber_x: Optional[Union[int, str]] = None,
                        fiber_y: Optional[Union[int, str]] = None,
                        width: Optional[Union[int, str]] = None,
                        height: Optional[Union[int, str]] = None,
                        binning: Union[int, str] = 1,
                        gain: Union[int, str] = 170) -> CanonicalResponse:

        if fiber_x is None and fiber_y is None and width is None and height is None:
            width = self.camera.cameraXSize
            height = self.camera.cameraYSize
            fiber_x = int(width / 2)
            fiber_y = int(height / 2)
        Thread(name='expose-roi-thread', target=self.do_expose_roi,
               args=[subfolder, exposure_seconds, repeats, seconds_between_exposures,
                     fiber_x, fiber_y, width, height, binning, gain]).start()
        return CanonicalResponse_Ok

    def do_expose_roi(self,
                      subfolder: Optional[str] = None,
                      exposure_seconds: Union[float, str] = 3,
                      repeats: Union[int, str] = 1,
                      seconds_between_exposures: Union[float, str] = 0,
                      fiber_x: Union[int, str] = 6000,
                      fiber_y: Union[int, str] = 2500,
                      width: Union[int, str] = 1500,
                      height: Union[int, str] = 1300,
                      binning: Union[int, str] = 1,
                      gain: Union[int, str] = 170) -> CanonicalResponse:
        op = function_name()

        seconds = float(exposure_seconds) if isinstance(exposure_seconds, str) else exposure_seconds
        repeats = int(repeats) if isinstance(repeats, str) else repeats
        seconds_between_exposures = float(seconds_between_exposures) \
            if isinstance(seconds_between_exposures, str) else seconds_between_exposures
        fiber_x = int(fiber_x) if isinstance(fiber_x, str) else fiber_x
        fiber_y = int(fiber_y) if isinstance(fiber_y, str) else fiber_y
        width = int(width) if isinstance(width, str) else width
        height = int(height) if isinstance(height, str) else height
        _binning = int(binning) if isinstance(binning, str) else binning
        gain = int(gain) if isinstance(gain, str) else gain

        if _binning not in [1, 2, 4]:
            return CanonicalResponse(errors=[f"bad {_binning=}, should be 1, 2 or 4"])

        self.mount.start_tracking()
        for repeat in range(repeats):
            end = None
            if seconds_between_exposures != 0.0:
                start = datetime.datetime.now()
                end = start + datetime.timedelta(seconds=seconds_between_exposures)

            unit_roi = UnitRoi(fiber_x, fiber_y, width, height)
            binning: CameraBinning = CameraBinning(_binning, _binning)
            default_folder = PathMaker().make_exposures_folder()
            base_folder = os.path.join(default_folder, subfolder) if subfolder else default_folder
            camera_settings = camera.CameraSettings(
                seconds=seconds,
                base_folder=base_folder,
                gain=gain,
                binning=binning,
                roi=unit_roi.to_camera_roi(binning=binning),
                tags={'expose-roi': None},
                save=True)
            logger.info(f"{op}: starting exposure #{repeat} (of {repeats})")
            self.camera.do_start_exposure(camera_settings)
            self.camera.wait_for_image_saved()
            filer.move_ram_to_shared(self.camera.latest_settings.image_path)

            if seconds_between_exposures != 0.0:
                now = datetime.datetime.now()
                if now < end:
                    period = (end - now).seconds
                    logger.info(f"{op}: sleeping {period} seconds till next exposure ...")
                    time.sleep(period)

        self.mount.stop_tracking()
        return CanonicalResponse_Ok

    def test_stage_repeatability(self,
                                 start_position: Union[int, str] = 50000,
                                 end_position: Union[int, str] = 300000,
                                 step: Union[int, str] = 25000,
                                 exposure_seconds: Union[int, str] = 5,
                                 binning: Union[int, str] = 1,
                                 gain: Union[int, str] = 170) -> CanonicalResponse:
        Thread(name='test-stage-repeatability', target=self.do_test_stage_repeatability,
               args=[start_position, end_position, step, exposure_seconds, binning, gain]).start()
        return CanonicalResponse_Ok

    def do_test_stage_repeatability(self,
                                    start_position: Union[int, str] = 50000,
                                    end_position: Union[int, str] = 300000,
                                    step: Union[int, str] = 25000,
                                    exposure_seconds: Union[int, str] = 5,
                                    binning: Union[int, str] = 1,
                                    gain: Union[int, str] = 170) -> CanonicalResponse:
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

        for position in range(start_position + step, end_position, step):
            logger.info(f"{op}: moving stage to {reference_position=}")
            self.stage.move_absolute(reference_position)
            while self.stage.is_active(StageActivities.Moving):
                time.sleep(.5)

            # expose at reference
            exposure_settings = camera.CameraSettings(
                seconds=exposure_seconds,
                base_folder=PathMaker().make_exposures_folder(),
                gain=gain,
                binning=CameraBinning(binning, binning),
                roi=None,
                tags={
                    "stage-repeatability": None,
                    "reference-for": position,
                }, save=True)

            self.camera.do_start_exposure(exposure_settings)
            self.camera.wait_for_image_saved()
            logger.info(f"{op}: reference image was saved")
            filer.move_ram_to_shared(exposure_settings.image_path)

            # expose at shifted position
            logger.info(f"{op}: moving stage to shifted {position=}")
            self.stage.move_absolute(position)
            while self.stage.is_active(StageActivities.Moving):
                time.sleep(.5)

            exposure_settings = camera.CameraSettings(
                seconds=exposure_seconds,
                base_folder=PathMaker().make_exposures_folder(),
                gain=gain,
                binning=CameraBinning(binning, binning),
                roi=None,
                tags={
                    "stage-repeatability": None,
                    "position": position,
                },
                save=True)
            self.camera.do_start_exposure(exposure_settings)
            self.camera.wait_for_image_saved()
            logger.info(f"{op}: image at {position=} was saved")
            filer.move_ram_to_shared(exposure_settings.image_path)

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
                target_ra=assignment.target.ra,
                target_dec=assignment.target.dec,
            )

            while self.is_active(UnitActivities.Autofocusing):
                time.sleep(10)

            #
            # If UnitActivities.Autofocusing ends in success, an autofocuser result should
            #  be available
            #
            if not self.autofocuser.latest_result:
                return  # should propagate errors as well

            self.controller_api.client.put(method='task_product_notification', data=TaskProduct(
                unit=self.name,
                ulid=assignment.task.ulid,
                type='autofocus',
                path=os.path.dirname(self.camera.latest_settings.image_path),
            ))

            #
            # At this point we have autofocused and can start acquisition
            #
            self.acquirer.start_acquisition_and_guiding(
                ra_j2000_hours=assignment.target.ra,
                dec_j2000_degs=assignment.target.dec
            )

    async def execute_assignment(self, assignment: UnitAssignmentModel):
        if not self.operational:
            return CanonicalResponse(errors=self.why_not_operational)

        if not self.is_idle():
            return CanonicalResponse(errors=[f"busy ({self.activities=})"])

        Thread(target=self.do_execute_assignment, args=[assignment]).start()

        return CanonicalResponse_Ok

    @staticmethod
    async def calculate_sky_pixel(fiber_x: float,
                                  fiber_y: float,
                                  slope_x: float,
                                  slope_y: float):

        cfg = Config().get_unit()
        stage_shift = cfg['stage']['presets']['sky'] - cfg['stage']['presets']['spec']

        cfg['acquisition']['roi']['sky_x'] = int(slope_x * stage_shift + fiber_x)
        cfg['acquisition']['roi']['sky_y'] = int(slope_y * stage_shift + fiber_y)
        cfg['guiding']['roi']['fiber_x'] = int(fiber_x)
        cfg['guiding']['roi']['fiber_y'] = int(fiber_y)

        Config().set_unit(unit_name=cfg['name'], unit_conf=cfg)

        return CanonicalResponse_Ok

    async def spiral_new_path(self, x_step_arcsec: float, y_step_arcsec: float):
        """
        Defines a new spiral path
        :param x_step_arcsec:
        :param y_step_arcsec:
        :return:
        """
        self.mount.pw.mount_spiral_offset_new(x_step_arcsec=x_step_arcsec, y_step_arcsec=y_step_arcsec)

    async def spiral_next_step(self):
        """
        Takes the next step in the currently defined spiral path
        :return:
        """
        await self.mount.pw.mount_spiral_offset_next()

    async def spiral_previous_step(self):
        """
        Goes back one step in the currently defined spiral path
        :return:
        """
        await self.mount.pw.mount_spiral_offset_previous()


def serialize_ip_addresses(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: serialize_ip_addresses(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [serialize_ip_addresses(item) for item in data]
    elif isinstance(data, ipaddress.IPv4Address):
        return str(data)
    else:
        return data


unit_id: int | str | None = None
hostname = socket.gethostname()
if hostname.startswith('mast'):
    try:
        unit_id = int(hostname[4:])
    except ValueError:
        unit_id = hostname[4:]
else:
    logger.error(f"Cannot figure out the MAST unit_id ({hostname=})")

base_path = BASE_UNIT_PATH
tag = 'Unit'

unit: Unit | None = None
if not unit:
    unit = Unit(id_=unit_id)


router = APIRouter()
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=unit.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=unit.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=unit.abort)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=unit.status)
router.add_api_route(base_path + '/start_autofocus', tags=[tag], endpoint=unit.autofocuser.start_autofocus)
router.add_api_route(base_path + '/stop_autofocus', tags=[tag], endpoint=unit.autofocuser.stop_autofocus)
router.add_api_route(base_path + '/stop_acquisition_and_guiding', tags=[tag],
                     endpoint=unit.guider.stop_acquisition_and_guiding)
router.add_api_route(base_path + '/start_acquisition_and_guiding', tags=[tag],
                     endpoint=unit.acquirer.start_acquisition_and_guiding)
router.add_api_route(base_path + '/expose', tags=[tag], endpoint=unit.expose_with_roi)
router.add_api_route(base_path + '/test_stage_repeatability', tags=[tag], endpoint=unit.test_stage_repeatability)
router.add_api_route(base_path + '/execute_assignment', methods=['PUT'], tags=[tag], endpoint=unit.execute_assignment)
router.add_api_route(base_path + '/calculate_sky_pixel', tags=[tag], endpoint=unit.calculate_sky_pixel)

tag = 'PlaneWave mount - spiral path'
router.add_api_route(base_path + '/spiral_new_path', tags=[tag], endpoint=unit.spiral_new_path)
router.add_api_route(base_path + '/spiral_next_step', tags=[tag], endpoint=unit.spiral_next_step)
router.add_api_route(base_path + '/spiral_previous_step', tags=[tag], endpoint=unit.spiral_previous_step)
