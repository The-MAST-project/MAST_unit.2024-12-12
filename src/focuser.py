import logging
from collections import deque
from enum import IntEnum, auto
from typing import TYPE_CHECKING

import win32com.client
from fastapi.routing import APIRouter

from common.activities import FocuserActivities
from common.ascom import AscomDispatcher, ascom_run
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.models.statuses import FocuserStatus
from common.utils import RepeatTimer, boxed_log, time_stamp
from PlaneWave import pwi4_client

if TYPE_CHECKING:
    pass

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)


class FocusDirection(IntEnum):
    In = auto()
    Out = auto()


class Focuser(Component, SwitchedOutlet, AscomDispatcher):

    _instance = None
    _initialized = False
    CLOSE_ENOUGH = 2

    @property
    def ascom(self) -> win32com.client.Dispatch:  # type: ignore
        return self._ascom

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, unit = None):
        if self._initialized:
            return

        self.unit = unit
        self.conf = Config().get_unit().focuser
        try:
            self._ascom = win32com.client.Dispatch(self.conf.ascom_driver)
        except Exception as ex:
            logger.exception(ex)
            raise ex

        SwitchedOutlet.__init__(self, OutletDomain.UnitOutlets, outlet_name="Focuser")
        Component.__init__(self)

        if not self.is_on():
            self.power_on()

        self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()
        self.connect()

        self.target: int | None = None
        self.lower_limit = 0
        self.upper_limit = 30000
        response = ascom_run(self, "MaxStep")
        if response.failed:
            logger.error(f"could not get MaxStep (failure={response.failure})")
        else:
            self.upper_limit = response.value

        self.known_as_good_position = self.conf.known_as_good_position
        logger.info(f"focuser: known_as_good_position: {self.known_as_good_position}")

        self._was_shut_down = False

        self.latest_positions = deque(maxlen=3)
        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = "focuser-timer-thread"
        self.timer.start()

        self._initialized = True
        logger.info("initialized")

    def position_sampler(self):
        return self.position

    def endpoint_startup(self):
        return self.startup()

    def startup(self):
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        self.pw.focuser_enable()
        self._was_shut_down = False
        if (
            self.known_as_good_position is not None
            and self.position != self.known_as_good_position
        ):
            self.position = self.known_as_good_position
        return CanonicalResponse_Ok

    def endpoint_shutdown(self):
        return self.shutdown()

    def shutdown(self):
        if self.connected:
            self.disconnect()
        self.pw.focuser_disable()
        if self.is_on():
            self.power_off()
        self._was_shut_down = True
        return CanonicalResponse_Ok

    def connect(self):
        if not self.is_on():
            self.power_on()

        ascom_run(self, "Connected = True")
        response = ascom_run(self, "Connected")
        if response.failed:
            logger.error(
                f"could not ASCOM Connected = True (failure={response.failure})"
            )
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
        return stat.focuser.is_connected  # type: ignore

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
        return round(stat.focuser.position)  # type: ignore

    @position.setter
    def position(self, value: int):
        if not self.is_on() or not self.connected:
            logger.error(f"Cannot goto {value} - not-powered or not-connected")
            return

        if self.close_enough(value):
            logger.info(f"at {self.position=} (close enough to {value=})")
        else:
            self.target = value
            self.start_activity(FocuserActivities.Moving, details=f"from {self.position} to {self.target}")
            self.pw.focuser_goto(value)

    def close_enough(self, position):
        return abs(self.position - position) <= self.CLOSE_ENOUGH

    def endpoint_set_position(self, position: int | str):
        """
        Sends the focuser to the specified position

        Parameters
        ----------
        position
            The target position
        """

        if isinstance(position, str):
            position = int(position)
        self.position = position
        return CanonicalResponse_Ok

    def endpoint_goto_known_as_good_position(self):
        """
        Go to the 'known-as-good' position
        :mastapi:
        """
        if self.known_as_good_position is None:
            return CanonicalResponse(errors=["known_as_good_position is None"])

        self.position = self.known_as_good_position
        return CanonicalResponse_Ok

    def endpoint_move_in(self, amount):
        self.move(amount, direction=FocusDirection.In)

    def endpoint_move_out(self, amount):
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
                return CanonicalResponse(errors=[msg])
        else:
            target = current_position + amount
            if self.upper_limit and target >= self.upper_limit:
                msg = f"target position ({target}) would be below upper limit ({self.upper_limit})"
                logger.error(msg)
                return CanonicalResponse(errors=[msg])

        self.position = target
        return CanonicalResponse_Ok

    def endpoint_abort(self):
        return self.abort()

    def abort(self):
        """
        Aborts any in-progress focuser activities
        """
        if self.is_active(FocuserActivities.Moving):
            self.pw.focuser_stop()
            self.end_activity(FocuserActivities.Moving)

        if self.is_active(FocuserActivities.StartingUp):
            self.end_activity(FocuserActivities.StartingUp)
        return CanonicalResponse_Ok

    @property
    def is_stationary(self) -> bool:
        return self.latest_positions.count == self.latest_positions.maxlen and \
            all(self.latest_positions[0] == pos for pos in self.latest_positions)

    def ontimer(self):
        if self.unit and self.unit.unit_shutdown_event.is_set():
            self.timer.cancel()
            return

        if not self.connected:
            return

        if self.is_active(FocuserActivities.Moving):
            if self.is_stationary and not self.close_enough(self.target):
                boxed_log(logger, [
                    "Focuser is stationary but not close_enough to target",
                    f"{self.target=}, {self.position=}, {self.CLOSE_ENOUGH=}",
                    f"Moving it to {self.target} again"
                    ], center=True)
                assert(self.target is not None)
                self.position = self.target

            elif self.close_enough(self.target):
                self.end_activity(FocuserActivities.Moving)
                self.target = None

    def endpoint_status(self) -> FocuserStatus | None:
        return self.status()

    def status(self) -> FocuserStatus | None:
        pw_stat = self.pw.status()
        ascom_response = ascom_run(self, "IsMoving")
        is_moving = (
            ascom_response.value
            if ascom_response.succeeded
            else pw_stat.focuser.is_moving  # type: ignore
        )

        return FocuserStatus(
            **self.power_status().model_dump(),
            **self.ascom_status().model_dump(),
            **self.component_status().model_dump(),
            lower_limit=self.lower_limit,
            upper_limit=self.upper_limit,
            known_as_good_position=self.known_as_good_position,
            position=self.position,
            target=self.target,
            target_verbal=f"{self.target}",
            moving=is_moving,  # type: ignore
            date=time_stamp(),
        )

    @property
    def name(self) -> str:
        return "focuser"

    @property
    def operational(self) -> bool:
        st = self.pw.status()
        return all(
            [
                not self.was_shut_down,
                self.is_on(),
                st.focuser.exists,  # type: ignore
                st.focuser.is_connected,  # type: ignore
            ]
        )

    @property
    def why_not_operational(self) -> list[str]:
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
                if not st.focuser.exists:  # type: ignore
                    ret.append(f"{self.name}: (via PWI4) - does not exist")
                elif not st.focuser.is_connected:  # type: ignore
                    ret.append(f"{self.name}: (via PWI4) - not connected")
        return ret

    @property
    def detected(self) -> bool:
        st = self.pw.status()
        return st.focuser.exists  # type: ignore

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    @property
    def api_router(self) -> APIRouter:

        base_path = Const.BASE_UNIT_PATH + "/focuser"
        tag = "Focuser"

        def endpoint_get_position():
            return self.position

        router = APIRouter()
        router.add_api_route(base_path + "/startup", tags=[tag], endpoint=self.endpoint_startup)
        router.add_api_route(
            base_path + "/shutdown", tags=[tag], endpoint=self.endpoint_shutdown
        )
        router.add_api_route(base_path + "/abort", tags=[tag], endpoint=self.endpoint_abort)
        router.add_api_route(base_path + "/status", tags=[tag], endpoint=self.endpoint_status)
        router.add_api_route(base_path + "/connect", tags=[tag], endpoint=self.connect)
        router.add_api_route(
            base_path + "/disconnect", tags=[tag], endpoint=self.disconnect
        )
        router.add_api_route(base_path + "/position", tags=[tag], endpoint=endpoint_get_position)
        router.add_api_route(
            base_path + "/position",
            methods=["PUT"],
            tags=[tag],
            endpoint=self.endpoint_set_position,
        )
        router.add_api_route(
            base_path + "/goto_known_as_good_position",
            tags=[tag],
            endpoint=self.endpoint_goto_known_as_good_position,
        )
        router.add_api_route(base_path + "/move", tags=[tag], endpoint=self.move)
        router.add_api_route(base_path + "/move_in", tags=[tag], endpoint=self.endpoint_move_in)
        router.add_api_route(
            base_path + "/move_out", tags=[tag], endpoint=self.endpoint_move_out
        )

        return router

if __name__ == "__main__":
    import time

    focuser = Focuser(unit=None)
    pos = focuser.position
    focuser.position = pos + 2000
    while focuser.is_active(FocuserActivities.Moving):
        time.sleep(1)
    pos = focuser.position
    focuser.position = pos - 1000
    while focuser.is_active(FocuserActivities.Moving):
        time.sleep(1)
    exit(0)
