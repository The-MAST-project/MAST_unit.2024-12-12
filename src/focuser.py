from collections import deque

import win32com.client
from fastapi.routing import APIRouter

from common.activities import FocuserActivities
from common.ascom import AscomDispatcher, ascom_run
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.endpoints import Completion, Tier, add_api_route, endpoint, register_component_endpoints
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.statuses import FocuserStatus
from common.utils import RepeatTimer, boxed_log, function_name, time_stamp
from PlaneWave import pwi4_client

logger = get_logger(__name__)


class Focuser(Component, SwitchedOutlet, AscomDispatcher):
    _instance = None
    _initialized = False
    CLOSE_ENOUGH = 2

    @property
    def ascom(self) -> win32com.client.Dispatch:  # type: ignore
        return self._ascom

    @property
    def conf(self):
        """This component's configuration, live.

        Was snapshotted in ``__init__``, which is why a value edited in the database
        reached a running unit only at the next service restart. Within one configuration
        generation this returns the same object every time, so it is a memo lookup, not a
        rebuild -- which is what makes a property affordable here.
        """
        assert self.unit is not None and self.unit.unit_conf is not None
        return self.unit.unit_conf.focuser

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, unit=None):
        if self._initialized:
            return

        self.unit = unit
        assert self.unit and self.unit.unit_conf is not None
        try:
            self._ascom = win32com.client.Dispatch(self.conf.ascom_driver)
        except Exception:
            logger.exception(f"could not create ASCOM focuser driver '{self.conf.ascom_driver}'")
            raise

        SwitchedOutlet.__init__(self, OutletDomain.UnitOutlets, outlet_name="Focuser")
        Component.__init__(self, FocuserActivities)

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

    @endpoint(tier=Tier.INTERFACE, completion=FocuserActivities.StartingUp)
    def startup(self):
        self.start_activity(FocuserActivities.StartingUp)
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        self.pw.focuser_enable()
        self._was_shut_down = False
        if self.known_as_good_position is not None and self.position != self.known_as_good_position:
            self.position = self.known_as_good_position  # `ontimer` ends StartingUp on arrival
        else:
            self.end_activity(FocuserActivities.StartingUp)
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.INTERFACE, completion=FocuserActivities.ShuttingDown)
    def shutdown(self):
        self.start_activity(FocuserActivities.ShuttingDown)
        if self.connected:
            self.disconnect()
        self.pw.focuser_disable()
        self.end_activity(FocuserActivities.ShuttingDown)
        return CanonicalResponse_Ok

    @property
    def is_shutting_down(self) -> bool:
        return self.is_active(FocuserActivities.ShuttingDown)

    def powerdown(self):
        if not self._was_shut_down:
            self.shutdown()
        while self.is_shutting_down:
            time.sleep(1)
        self.power_off()

    def connect(self):
        if not self.is_on():
            self.power_on()

        ascom_run(self, "Connected = True")
        response = ascom_run(self, "Connected")
        if response.failed:
            logger.error(f"could not ASCOM Connected = True (failure={response.failure})")
            self.connected = False
        else:
            self.connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
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
        stat = self.pw.status()
        return round(stat.focuser.position)  # type: ignore

    @position.setter
    def position(self, value: int):
        self.goto_position(value)

    def goto_position(self, value: int) -> CanonicalResponse:
        """
        Sends the focuser to an absolute position, reporting refusals to the caller.

        The ``position`` setter cannot return anything (assignment discards it), so the
        decision lives here and the setter delegates -- otherwise a not-powered or
        not-connected focuser silently reports success over HTTP.
        """
        if not self.is_on() or not self.connected:
            msg = f"Cannot goto {value} - not-powered or not-connected"
            logger.error(msg)
            return CanonicalResponse(errors=[msg])

        if self.close_enough(value):
            logger.info(f"at {self.position=} (close enough to {value=})")
        else:
            self.target = value
            # Stationarity must be concluded from samples taken after the command: the
            # pre-move readings are all equal, so keeping them would report the focuser as
            # settled before it has been asked to move.
            self.latest_positions.clear()
            self.start_activity(FocuserActivities.Moving, details=[f"from {self.position} to {self.target}"])
            self.pw.focuser_goto(value)
        return CanonicalResponse_Ok

    def close_enough(self, position):
        return abs(self.position - position) <= self.CLOSE_ENOUGH

    @endpoint(tier=Tier.OPERATION, completion=FocuserActivities.Moving)
    def endpoint_set_position(self, position: int | str):
        """
        Sends the focuser to the specified position

        Parameters
        ----------
        position
            The target position
        """

        if isinstance(position, str):
            try:
                position = int(position)
            except ValueError:
                # Matches stage.move_absolute, the sibling absolute-move handler. Without it
                # the refusal reads as a bare ValueError from int(), which names the exception
                # rather than what was rejected.
                return CanonicalResponse(errors=[f"{function_name()}: '{position}' is not a position"])
        return self.goto_position(position)

    @endpoint(tier=Tier.OPERATION, completion=FocuserActivities.Moving)
    def endpoint_goto_known_as_good_position(self):
        """
        Go to the 'known-as-good' position
        """
        if self.known_as_good_position is None:
            return CanonicalResponse(errors=["known_as_good_position is None"])

        return self.goto_position(self.known_as_good_position)

    @endpoint(tier=Tier.OPERATION, completion=FocuserActivities.Moving)
    def move_relative(self, amount: int):
        """
        Move the focuser by a signed amount: positive is outward, negative is inward.

        Parameters
        ----------
        amount
            How far to move, and which way. The sign carries the direction that `move_in` and
            `move_out` used to carry as separate routes (#41).
        """
        target = self.position + amount
        if target < self.lower_limit:
            msg = f"target position ({target}) would be below lower limit ({self.lower_limit})"
            logger.error(msg)
            return CanonicalResponse(errors=[msg])
        if self.upper_limit and target >= self.upper_limit:
            msg = f"target position ({target}) would be above upper limit ({self.upper_limit})"
            logger.error(msg)
            return CanonicalResponse(errors=[msg])

        return self.goto_position(target)

    @endpoint(tier=Tier.INTERFACE, completion=FocuserActivities.Aborting)
    def abort(self):
        """
        Aborts any in-progress focuser activities
        """
        was_moving = self.is_active(FocuserActivities.Moving)
        if was_moving:
            self.end_activity(FocuserActivities.Moving)

        if self.is_active(FocuserActivities.StartingUp):
            self.end_activity(FocuserActivities.StartingUp)

        if was_moving:
            self.start_activity(FocuserActivities.Aborting)
            self.pw.focuser_stop()
        return CanonicalResponse_Ok

    @property
    def is_stationary(self) -> bool:
        return len(self.latest_positions) == self.latest_positions.maxlen and all(
            self.latest_positions[0] == pos for pos in self.latest_positions
        )

    def ontimer(self):
        if self.unit and self.unit.unit_shutdown_event.is_set():
            self.timer.cancel()
            return

        if not self.connected:
            return

        self.latest_positions.append(self.position)

        if self.is_active(FocuserActivities.Moving):
            if self.is_stationary and not self.close_enough(self.target):
                boxed_log(
                    logger,
                    [
                        "Focuser is stationary but not close_enough to target",
                        f"{self.target=}, {self.position=}, {self.CLOSE_ENOUGH=}",
                        f"Moving it to {self.target} again",
                    ],
                    center=True,
                )
                assert self.target is not None
                self.position = self.target

            elif self.close_enough(self.target):
                self.end_activity(FocuserActivities.Moving)
                self.target = None

        if self.is_active(FocuserActivities.StartingUp) and self.close_enough(self.known_as_good_position):
            self.end_activity(FocuserActivities.StartingUp)

        # Position stability, not PWI4's `focuser.is_moving`: that stays true indefinitely
        # after `focuser_stop()`, so the flag could never come down (#163, measured on mast02).
        if self.is_active(FocuserActivities.Aborting) and self.is_stationary:
            self.end_activity(FocuserActivities.Aborting)

    @endpoint(tier=Tier.INTERFACE, completion=Completion.IMMEDIATE)
    def status(self) -> FocuserStatus | None:
        pw_stat = self.pw.status()
        ascom_response = ascom_run(self, "IsMoving")
        is_moving = (
            ascom_response.value if ascom_response.succeeded else pw_stat.focuser.is_moving  # type: ignore
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

    @endpoint(tier=Tier.OPERATION, completion=Completion.IMMEDIATE)
    def get_position(self) -> int | None:
        # Enveloped at registration (#34 stage 3).
        return self.position

    @property
    def api_router(self) -> APIRouter:

        base_path = Const.BASE_UNIT_PATH + "/focuser"

        router = APIRouter()
        register_component_endpoints(router, self, base_path)
        add_api_route(router, base_path + "/position", endpoint=self.get_position)
        add_api_route(
            router,
            base_path + "/position",
            methods=["PUT"],
            endpoint=self.endpoint_set_position,
        )
        add_api_route(
            router,
            base_path + "/goto_known_as_good_position",
            endpoint=self.endpoint_goto_known_as_good_position,
            methods=["PUT"],
        )
        add_api_route(router, base_path + "/move_relative", endpoint=self.move_relative, methods=["PUT"])

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
