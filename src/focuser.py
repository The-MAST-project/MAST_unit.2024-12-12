from typing import List
import logging
from enum import IntEnum, auto
import win32com.client

from common.utils import RepeatTimer, Component, time_stamp, CanonicalResponse, CanonicalResponse_Ok, BASE_UNIT_PATH
from common.config import Config
from common.mast_logging import init_log
from PlaneWave import pwi4_client
from common.dlipowerswitch import SwitchedOutlet, OutletDomain
from fastapi.routing import APIRouter
from common.ascom import ascom_run, AscomDispatcher
from common.activities import FocuserActivities
from common.stopping import StoppingMonitor

logger = logging.getLogger('mast.unit.' + __name__)
init_log(logger)


class FocusDirection(IntEnum):
    In = auto()
    Out = auto()


class Focuser(Component, SwitchedOutlet, AscomDispatcher, StoppingMonitor):

    _instance = None
    _initialized = False

    @property
    def ascom(self) -> win32com.client.Dispatch:
        return self._ascom

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Focuser, cls).__new__(cls)
        return cls._instance

    def __init__(self, unit: 'Unit'):
        if self._initialized:
            return

        self.unit = unit
        self.unit_conf = Config().get_unit()
        self.conf = self.unit_conf['focuser']
        try:
            self._ascom = win32com.client.Dispatch(self.conf['ascom_driver'])
        except Exception as ex:
            logger.exception(ex)
            raise ex

        SwitchedOutlet.__init__(self, OutletDomain.Unit, outlet_name='Focuser')
        Component.__init__(self)
        # StoppingMonitor.__init__(self, 'focuser', max_len=5, sampler=self.position_sampler, interval=1, epsilon=0)

        if not self.is_on():
            self.power_on()

        self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()
        self.connect()

        self.target: int | None = None
        self.lower_limit = 0
        self.upper_limit = 30000
        response = ascom_run(self, 'MaxStep')
        if response.failed:
            logger.error(f"could not get MaxStep (failure={response.failure})")
        else:
            self.upper_limit = response.value

        self.known_as_good_position: int | None = \
            int(self.conf['known_as_good_position']) if 'known_as_good_position' in self.conf \
            else int(self.upper_limit / 2) if self.upper_limit \
            else None
        logger.info(f"focuser: known_as_good_position: {self.known_as_good_position}")

        self._was_shut_down = False
        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'focuser-timer-thread'
        self.timer.start()

        self._initialized = True
        logger.info('initialized')

    def position_sampler(self):
        return self.position

    def startup(self):
        """
        :mastapi:
        """
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        self.pw.focuser_enable()
        self._was_shut_down = False
        if self.known_as_good_position is not None and self.position != self.known_as_good_position:
            self.position = self.known_as_good_position
        return CanonicalResponse_Ok

    def shutdown(self):
        """
        :mastapi:
        """
        if self.connected:
            self.disconnect()
        self.pw.focuser_disable()
        if self.is_on():
            self.power_off()
        self._was_shut_down = True
        return CanonicalResponse_Ok

    def connect(self):
        """
        :mastapi:
        """
        if not self.is_on():
            self.power_on()

        ascom_run(self, 'Connected = True')
        response = ascom_run(self, 'Connected')
        if response.failed:
            logger.error(f"could not ASCOM Connected = True (failure={response.failure})")
            self.connected = False
        else:
            self.connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        :mastapi:
        """
        self.connected = False
        return CanonicalResponse_Ok

    @property
    def connected(self):
        stat = self.pw.status()
        return stat.focuser.is_connected  # and self.ascom and self.ascom.Connected

    @connected.setter
    def connected(self, value):
        if value:
            self.pw.focuser_enable()
            self.pw.focuser_connect()
        else:
            self.pw.focuser_disconnect()
            self.pw.focuser_disable()

        # if self.ascom:
        #     response = ascom_run(self, f'Connected = {value}', True)
        #     if response.failed:
        #         logger.error(f"failed to connect (failure=0x{response.failure:08X})")

    @property
    def position(self) -> int:
        """
        :mastapi:
        """
        stat = self.pw.status()
        return round(stat.focuser.position)

    @position.setter
    def position(self, value: int):
        if not self.is_on() or not self.connected:
            logger.error(f'Cannot goto {value} - not-powered or not-connected')
            return

        if self.close_enough(value):
            logger.info(f"at {self.position=} (close enough to {value=})")
        else:
            self.target = value
            self.start_activity(FocuserActivities.Moving)
            self.pw.focuser_goto(value)

    def close_enough(self, position):
        return abs(self.position - position) <= 2

    def set_position(self, position: int | str):
        """
        Sends the focuser to the specified position

        Parameters
        ----------
        position
            The target position

        :mastapi:
        """

        if isinstance(position, str):
            position = int(position)
        self.position = position
        return CanonicalResponse_Ok

    def goto_known_as_good_position(self):
        """
        Go to the 'known-as-good' position
        :mastapi:
        """
        self.position = self.known_as_good_position
        return CanonicalResponse_Ok

    def move_in(self, amount):
        self.move(amount, direction=FocusDirection.In)

    def move_out(self, amount):
        self.move(amount, direction=FocusDirection.Out)

    def move(self, amount: int, direction: FocusDirection):
        """
        Move the focuser in or out by the specified amount

        Parameters
        ----------
        amount
            How much to move
        direction
            Either In or Out

        :mastapi:
        """
        current_position = self.position
        if direction == FocusDirection.In:
            target = current_position - amount
            if target < self.lower_limit:
                msg = f"target position ({target}) would be below lower limit ({self.lower_limit})"
                logger.error(msg)
                return CanonicalResponse(errors=msg)
        else:
            target = current_position + amount
            if target >= self.upper_limit:
                msg = f"target position ({target}) would be below upper limit ({self.upper_limit})"
                logger.error(msg)
                return CanonicalResponse(errors=msg)

        self.position = target
        return CanonicalResponse_Ok

    def abort(self):
        """
        Aborts any in-progress focuser activities

        :mastapi:
        Returns
        -------

        """
        if self.is_active(FocuserActivities.Moving):
            self.pw.focuser_stop()
            self.end_activity(FocuserActivities.Moving)

        if self.is_active(FocuserActivities.StartingUp):
            self.end_activity(FocuserActivities.StartingUp)
        return CanonicalResponse_Ok

    def ontimer(self):

        if self.is_active(FocuserActivities.Moving) and self.close_enough(self.target):
            self.end_activity(FocuserActivities.Moving)
            self.target = None

    def status(self) -> dict:
        """

        :mastapi:
        Returns
        -------
            FocuserStatus

        """
        stat = self.pw.status()
        ret = self.power_status() | self.ascom_status() | self.component_status()
        response = ascom_run(self, 'IsMoving')
        is_moving = response.value if response.succeeded else stat.focuser.is_moving
        ret |= {
            'lower_limit': self.lower_limit,
            'upper_limit': self.upper_limit,
            'known_as_good_position': self.known_as_good_position,
            'position': self.position,
            'target': self.target,
            'target_verbal': f"{self.target}",
            'moving': is_moving,
        }
        time_stamp(ret)
        return ret

    @property
    def name(self) -> str:
        return 'focuser'

    @property
    def operational(self) -> bool:
        st = self.pw.status()
        return all([not self.was_shut_down, self.is_on(), st.focuser.exists, st.focuser.is_connected])

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.is_on():
            ret.append(f"{self.name}: not powered")
        else:
            if self.was_shut_down:
                ret.append(f"{self.name}: shut down")
            if not self.detected:
                ret.append(f"{self.name}: not detected")
            else:
                st = self.pw.status()
                if not st.focuser.exists:
                    ret.append(f"{self.name}: (PWI4) - does not exist")
                elif not st.focuser.is_connected:
                    ret.append(f"{self.name}: (PWI4) - not connected")
        return ret

    @property
    def detected(self) -> bool:
        st = self.pw.status()
        return st.focuser.exists

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down


def get_position():
    return focuser.position


base_path = BASE_UNIT_PATH + "/focuser"
tag = 'Focuser'

focuser = Focuser(unit=None)

router = APIRouter()
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=focuser.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=focuser.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=focuser.abort)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=focuser.status)
router.add_api_route(base_path + '/connect', tags=[tag], endpoint=focuser.connect)
router.add_api_route(base_path + '/disconnect', tags=[tag], endpoint=focuser.disconnect)
router.add_api_route(base_path + '/position', tags=[tag], endpoint=get_position)
router.add_api_route(base_path + '/position', methods=['PUT'], tags=[tag], endpoint=focuser.set_position)
router.add_api_route(base_path + '/goto_known_as_good_position', tags=[tag],
                     endpoint=focuser.goto_known_as_good_position)
router.add_api_route(base_path + '/move', tags=[tag], endpoint=focuser.move)
router.add_api_route(base_path + '/move_in', tags=[tag], endpoint=focuser.move_in)
router.add_api_route(base_path + '/move_out', tags=[tag], endpoint=focuser.move_out)
