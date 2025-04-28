from logging import Logger

import win32com.client
import logging
from enum import Enum
from typing import List, Optional

from common.utils import RepeatTimer, time_stamp, CanonicalResponse_Ok, BASE_UNIT_PATH
from common.components import Component, ComponentStatus
from common.config import Config
from common.mast_logging import init_log
from common.dlipowerswitch import SwitchedOutlet, OutletDomain, PowerStatus
from fastapi.routing import APIRouter

from common.ascom import ascom_run, AscomDispatcher, AscomStatus
from common.activities import CoverActivities

logger: logging.Logger = logging.getLogger('mast.unit.' + __name__)
init_log(logger)


# https://ascom-standards.org/Help/Developer/html/T_ASCOM_DeviceInterface_CoverStatus.htm
class CoversState(Enum):
    NotPresent = 0
    Closed = 1
    Moving = 2
    Open = 3
    Unknown = 4
    Error = 5


class CoverStatus(PowerStatus, AscomStatus, ComponentStatus):
    target_verbal: Optional[str] = None
    state: Optional[CoversState] = None
    state_verbal: Optional[str] = None
    date: Optional[str] = None


class Covers(Component, SwitchedOutlet, AscomDispatcher):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Covers, cls).__new__(cls)
        return cls._instance
    """
    Uses the PlaneWave ASCOM driver for the **MAST** mirror covers
    """

    @property
    def ascom(self) -> win32com.client.Dispatch:
        return self._ascom

    @property
    def logger(self) -> Logger:
        # return logger
        return logger

    def __init__(self, unit: 'Unit'):
        if self._initialized:
            return

        self.unit = unit
        self.unit_conf: dict = Config().get_unit()
        self.conf = self.unit_conf['covers']
        try:
            self._ascom = win32com.client.Dispatch(self.conf['ascom_driver'])
        except Exception as ex:
            # logger.exception(ex)
            logger.exception(ex)
            raise ex

        SwitchedOutlet.__init__(self, OutletDomain.Unit, outlet_name='Covers')
        Component.__init__(self)

        # if not self.is_on():
        #     self.power_on()

        self.timer: RepeatTimer = RepeatTimer(2, self.ontimer)
        self.timer.name = 'covers-timer-thread'
        self.timer.start()

        self._connected: bool = False
        self._was_shut_down = False

        self._initialized = True
        logger.info('initialized')

    def connect(self):
        """
        Connects to the **MAST** mirror cover controller

        :mastapi:
        """
        response = ascom_run(self, 'Connected = True')
        if response.failed:
            logger.error(f"failed to connect {response.failure=}")
            self._connected = False
        else:
            self._connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        Disconnects from the **MAST** mirror cover controller
        :mastapi:
        """
        self.connected = False
        return CanonicalResponse_Ok

    @property
    def connected(self):
        # if self.ascom:
        #     return self.ascom.Connected
        # else:
        #     return False
        return self._connected

    @connected.setter
    def connected(self, value):
        logger.info(f"connected = {value}")
        try:
            response = ascom_run(self, f'Connected = {value}')
            if response.succeeded:
                self._connected = value
        finally:
            self._connected = False

    @property
    def state(self) -> CoversState:
        if not self.connected:
            return CoversState.NotPresent

        response = ascom_run(self, 'CoverState')
        if response.succeeded:
            return CoversState(response.value)
        else:
            return CoversState.Error

    # def status(self) -> dict:
    def status(self) -> CoverStatus:
        """
        :mastapi:
        """

        return CoverStatus(
            **self.power_status().dict(),
            **self.ascom_status().dict(),
            **self.component_status().dict(),
            state=self.state,
            state_verbal=self.state.__repr__(),
            target_verbal='Open' if self.is_active(CoverActivities.Opening) else
            'Close' if self.is_active(CoverActivities.Closing) else None,
            date=time_stamp(),
        )

    def open(self):
        """
        Starts opening the **MAST** mirror covers

        :mastapi:
        """
        if not self.connected:
            return

        logger.info('opening covers')
        self.start_activity(CoverActivities.Opening)
        response = ascom_run(self, 'OpenCover()')
        if response.failed:
            logger.error(f"failed to open covers (failure='{response.failure}')")
        return CanonicalResponse_Ok

    def close(self):
        """
        Starts closing the **MAST** mirror covers
        :mastapi:
        """
        if not self.connected:
            return

        logger.info('closing covers')
        self.start_activity(CoverActivities.Closing)
        response = ascom_run(self, 'CloseCover()')
        if response.failed:
            logger.error(f"failed to close covers (failure='{response.failure}')")
        return CanonicalResponse_Ok

    def startup(self):
        """
        Performs the ``startup`` routine for the **MAST** mirror covers controller

        :mastapi:
        """
        self._was_shut_down = False
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        if self.connected and self.state != CoversState.Open:
            self.start_activity(CoverActivities.StartingUp)
            self.open()
        return CanonicalResponse_Ok

    def shutdown(self):
        """
        Performs the ``shutdown`` procedure for the **MAST** mirror covers controller

        :mastapi:
        """
        if not self.connected:
            self.power_off()
            return

        if self.state != CoversState.Closed:
            self.start_activity(CoverActivities.ShuttingDown)
            self.close()
        return CanonicalResponse_Ok

    def abort(self):
        """
        :mastapi:
        Returns
        -------

        """
        response = ascom_run(self, 'HaltCover()')
        if response.failed:
            logger.error(f"failed to halt covers (failure='{response.failure}')")
        for activity in (CoverActivities.StartingUp, CoverActivities.ShuttingDown,
                         CoverActivities.Closing, CoverActivities.Opening):
            if self.is_active(activity):
                self.end_activity(activity)
        return CanonicalResponse_Ok

    def ontimer(self):
        if not self.connected:
            return

        # logger.debug(f"activities: {self.activities}, state: {self.state()}")
        if self.is_active(CoverActivities.Opening) and self.state == CoversState.Open:
            self.end_activity(CoverActivities.Opening)
            if self.is_active(CoverActivities.StartingUp):
                self.end_activity(CoverActivities.StartingUp)

        if self.is_active(CoverActivities.Closing) and self.state == CoversState.Closed:
            self.end_activity(CoverActivities.Closing)
            if self.is_active(CoverActivities.ShuttingDown):
                self.end_activity(CoverActivities.ShuttingDown)
                self._was_shut_down = True
                self.power_off()

    @property
    def name(self) -> str:
        return 'covers'

    @property
    def operational(self) -> bool:
        return all([self.is_on(), self.detected, self.ascom, self.connected, self.state == CoversState.Open])

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.is_on():
            ret.append(f"{self.name}: not powered")
        elif not self.detected:
            ret.append(f"{self.name}: (ASCOM) not detected")
        else:
            if not self.ascom:
                ret.append(f"{self.name}: (ASCOM) - no handle")
            else:
                if not self.connected:
                    ret.append(f"{self.name}: (ASCOM) - not connected")
                else:
                    state = self.state
                    if self.state != CoversState.Open:
                        ret.append(f"{self.name}: not open (state='{state.name}')")
        return ret

    @property
    def detected(self) -> bool:
        return self.connected
    
    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down


base_path = BASE_UNIT_PATH + "/covers"
tag = 'Covers'

covers = Covers(unit=None)

router = APIRouter()
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=covers.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=covers.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=covers.abort)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=covers.status)
router.add_api_route(base_path + '/connect', tags=[tag], endpoint=covers.connect)
router.add_api_route(base_path + '/disconnect', tags=[tag], endpoint=covers.disconnect)
router.add_api_route(base_path + '/open', tags=[tag], endpoint=covers.open)
router.add_api_route(base_path + '/close', tags=[tag], endpoint=covers.close)
