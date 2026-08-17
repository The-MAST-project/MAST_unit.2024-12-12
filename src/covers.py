import logging
import time
from logging import Logger
from typing import TYPE_CHECKING

import win32com.client
from fastapi.routing import APIRouter

from common.activities import CoverActivities
from common.ascom import AscomDispatcher, ascom_run
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.endpoints import Completion, Stability, Tier, add_api_route, endpoint, register_component_endpoints
from common.interfaces.components import Component
from common.models.statuses import CoversState, CoverStatus
from common.utils import RepeatTimer, time_stamp

if TYPE_CHECKING:
    from unit import Unit

logger: logging.Logger = logging.getLogger("mast.unit." + __name__)


class Covers(Component, SwitchedOutlet, AscomDispatcher):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    """
    Uses the PlaneWave ASCOM driver for the **MAST** mirror covers
    """

    @property
    def ascom(self) -> win32com.client.Dispatch:  # type: ignore
        return self._ascom

    @property
    def logger(self) -> Logger:
        # return logger
        return logger

    def __init__(self, unit: "Unit"):  # type: ignore[name]
        if self._initialized:
            return

        self.unit = unit
        assert self.unit is not None and self.unit.unit_conf is not None
        self.conf = self.unit.unit_conf.covers
        try:
            self._ascom = win32com.client.Dispatch(self.conf.ascom_driver)
        except Exception:
            logger.exception(f"could not create ASCOM covers driver '{self.conf.ascom_driver}'")
            raise

        SwitchedOutlet.__init__(self, OutletDomain.UnitOutlets, outlet_name="Covers")
        Component.__init__(self, CoverActivities)
        self._connected: bool = False
        self.activities = CoverActivities(0)

        if not self.is_on():
            self.power_on()

        self.timer: RepeatTimer = RepeatTimer(2, self.ontimer)
        self.timer.name = "covers-timer-thread"
        self.timer.start()

        self._connected: bool = False
        self._was_shut_down = False

        self._initialized = True
        logger.info("initialized")

    @endpoint(tier=Tier.OPERATION, stability=Stability.DEPRECATED, completion=Completion.IMMEDIATE)
    def connect(self):
        """
        Connects to the **MAST** mirror cover controller

        :mastapi:
        """
        response = ascom_run(self, "Connected = True")
        if response.failed:
            logger.error(f"failed to connect {response.failure=}")
            self._connected = False
        else:
            self._connected = True
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION, stability=Stability.DEPRECATED, completion=Completion.IMMEDIATE)
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
            response = ascom_run(self, f"Connected = {value}")
            if response.succeeded:
                self._connected = value
        finally:
            self._connected = False

    @property
    def state(self) -> CoversState:
        if not self.connected:
            return CoversState.NotPresent

        response = ascom_run(self, "CoverState")
        if response.succeeded:
            return CoversState(response.value)
        else:
            return CoversState.Error

    @endpoint(tier=Tier.INTERFACE, completion=Completion.IMMEDIATE)
    def status(self) -> CoverStatus:
        """
        :mastapi:
        """

        return CoverStatus(
            **self.power_status().model_dump(),
            **self.ascom_status().model_dump(),
            **self.component_status().model_dump(),
            state=self.state,
            state_verbal=self.state.__repr__(),
            target_verbal=(
                "Open"
                if self.is_active(CoverActivities.Opening)
                else "Close"
                if self.is_active(CoverActivities.Closing)
                else None
            ),
            date=time_stamp(),
        )

    @endpoint(tier=Tier.OPERATION, completion=CoverActivities.Opening)
    def endpoint_open(self):
        return self.open()

    def open(self):
        """
        Starts opening the **MAST** mirror covers

        :mastapi:
        """
        if not self.connected:
            return CanonicalResponse(errors=["not connected"])

        logger.info("opening covers")
        self.start_activity(CoverActivities.Opening)
        response = ascom_run(self, "OpenCover()")
        if response.failed:
            logger.error(f"failed to open covers (failure='{response.failure}')")
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION, completion=CoverActivities.Closing)
    def endpoint_close(self):
        return self.close()

    def close(self):
        """
        Starts closing the **MAST** mirror covers
        :mastapi:
        """
        if not self.connected:
            return CanonicalResponse(errors=["not connected"])

        logger.info("closing covers")
        self.start_activity(CoverActivities.Closing)
        response = ascom_run(self, "CloseCover()")
        if response.failed:
            logger.error(f"failed to close covers (failure='{response.failure}')")
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.INTERFACE, completion=CoverActivities.StartingUp)
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

    @endpoint(tier=Tier.INTERFACE, completion=CoverActivities.ShuttingDown)
    def shutdown(self):
        """
        Performs the ``shutdown`` procedure for the **MAST** mirror covers controller
        """
        if not self.connected:
            # Powering off *is* the shutdown for a disconnected cover -- success, not a refusal.
            self.power_off()
            return CanonicalResponse_Ok

        if self.state != CoversState.Closed:
            self.start_activity(CoverActivities.ShuttingDown)
            self.close()
        return CanonicalResponse_Ok

    @property
    def is_shutting_down(self) -> bool:
        return self.is_active(CoverActivities.ShuttingDown)

    def powerdown(self):
        if not self._was_shut_down:
            self.shutdown()
        while self.is_shutting_down:
            time.sleep(1)
        self.power_off()

    @endpoint(tier=Tier.INTERFACE, completion=CoverActivities.Aborting)
    def abort(self):
        """
        :mastapi:
        Returns
        -------

        """
        was_moving = any(
            self.is_active(activity)
            for activity in (
                CoverActivities.StartingUp,
                CoverActivities.ShuttingDown,
                CoverActivities.Closing,
                CoverActivities.Opening,
            )
        )

        response = ascom_run(self, "HaltCover()")
        if response.failed:
            logger.error(f"failed to halt covers (failure='{response.failure}')")
        for activity in (
            CoverActivities.StartingUp,
            CoverActivities.ShuttingDown,
            CoverActivities.Closing,
            CoverActivities.Opening,
        ):
            if self.is_active(activity):
                self.end_activity(activity)

        if was_moving:
            self.start_activity(CoverActivities.Aborting)
        return CanonicalResponse_Ok

    def ontimer(self):
        if self.unit.unit_shutdown_event.is_set():
            self.timer.cancel()
            return

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

        self._end_abort_when_at_rest()

    def _end_abort_when_at_rest(self) -> None:
        """End `Aborting` once the covers leave `Moving`. `Error` and `Unknown` are at rest too."""
        if self.is_active(CoverActivities.Aborting) and self.state != CoversState.Moving:
            self.end_activity(CoverActivities.Aborting)

    @property
    def name(self) -> str:
        return "covers"

    @property
    def operational(self) -> bool:
        return all(
            [
                self.is_on(),
                self.detected,
                self.ascom,
                self.connected,
                self.state == CoversState.Open,
            ]
        )

    @property
    def why_not_operational(self) -> list[str]:
        ret = []
        if not self.is_on():
            ret.append(f"{self.name}: not powered")
        elif not self.detected:
            ret.append(f"{self.name}: (via ASCOM) not detected")
        else:
            if not self.ascom:
                ret.append(f"{self.name}: (via ASCOM) - no handle")
            else:
                if not self.connected:
                    ret.append(f"{self.name}: (via ASCOM) - not connected")
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

    @property
    def api_router(self) -> APIRouter:
        """
        Returns the API router for the Covers component.
        :return: APIRouter instance with Covers endpoints
        """
        base_path = Const.BASE_UNIT_PATH + "/covers"

        router = APIRouter()
        register_component_endpoints(router, self, base_path)
        add_api_route(router, base_path + "/connect", endpoint=self.connect)
        add_api_route(router, base_path + "/disconnect", endpoint=self.disconnect)
        add_api_route(router, base_path + "/open", endpoint=self.endpoint_open, methods=["PUT"])
        add_api_route(router, base_path + "/close", endpoint=self.endpoint_close, methods=["PUT"])

        return router
