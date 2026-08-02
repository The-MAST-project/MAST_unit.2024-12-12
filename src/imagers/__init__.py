import time

import numpy as np
from fastapi import APIRouter
from pydantic import BaseModel

from common.canonical import CanonicalResponse
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.imager import ImagerExposureSeries, ImagerInterface, ImagerTypes
from common.mast_logging import get_logger
from common.models.statuses import ImagerSettings, ImagerStatus

logger = get_logger(__name__)
__all__ = ["Imager"]


class Imager(ImagerInterface, SwitchedOutlet):
    """
    This is the base class for all imagers.
    It provides the common interface and some common functionality.
    """

    _instance = None
    _initialized = False

    @staticmethod
    def valid_imager_types() -> list[str]:
        from common.config import Config

        valid_types = []

        unit_conf = Config().get_unit()
        assert unit_conf is not None

        for t in unit_conf.imager.valid_imager_types:
            valid_types.append("ascom" if t.startswith("ascom") else t)
        return valid_types

    @staticmethod
    def configured_imager():
        from common.config import Config

        unit_conf = Config().get_unit()
        assert unit_conf is not None

        return unit_conf.imager.imager_type

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        unit=None,
        imager_type: str | None = None,
        params: dict | None = None,  # type: ignore
    ):
        """
        Initializes the backend of an Imager instance according to the unit configuration.

        :param unit: "Unit" instance
        :param imager_type: The type of imager to use, e.g., "ascom:prog_id", "phd2", "zwo"
        :param imager_params: Parameters for the imager
        """
        if self._initialized:
            return  # already initialized, do not re-initialize

        SwitchedOutlet.group(
            domain=OutletDomain.UnitOutlets, group_name="Camera", outlet_names=["Camera", "CameraUSB"]
        ).transfer_attributes(self)
        if not self.is_on():
            self.power_on()

        ImagerInterface.__init__(self)

        self.unit = unit
        if unit and unit.unit_conf is None:
            self.conf = unit.unit_conf.imager
        else:
            from common.config import Config

            unit_conf = Config().get_unit()
            assert unit_conf is not None

            self.conf = unit_conf.imager

        imager_type = imager_type or self.conf.imager_type.lower()
        if not (imager_type.startswith("ascom") or (imager_type in Imager.valid_imager_types())):
            raise ValueError(f"bad imager type '{imager_type}', must be one of {Imager.valid_imager_types()}")

        if imager_type.startswith("ascom:"):
            from imagers.ascom import ASCOMImager

            self._prog_id = imager_type[6:]
            self._backend = ASCOMImager(parent_imager=self, prog_id=self._prog_id, _from_imager=True)
            self.backend_type = ImagerTypes.Ascom
        elif imager_type == "phd2":
            from phd2 import phd2

            self._backend = phd2.PHD2Connector(parent=self, _from_imager=True)
            self.backend_type = ImagerTypes.Phd2
        elif imager_type == "zwo":
            from zwo import ZWOImager

            self._backend = ZWOImager(parent_imager=self, imager_params=params, _from_imager=True)
            self.backend_type = ImagerTypes.Zwo
        else:
            raise ValueError(f"Unknown imager type: {self.conf.imager_type}")

        self.imager_params = params
        self.latest_settings: ImagerSettings | None = None
        self.current_exposure_series: ImagerExposureSeries | None = None
        self._initialized = True

    def __repr__(self):
        return f"Imager(_backend='{self._backend.__repr__()}')"

    @property
    def can_send_image_ready_event(self) -> bool:
        return self._backend.can_send_image_ready_event

    @property
    def can_send_image_saved_event(self) -> bool:
        return self._backend.can_send_image_saved_event

    def endpoint_startup(self) -> CanonicalResponse | None:
        return self.startup()

    def startup(self) -> CanonicalResponse | None:
        return self._backend.startup()

    def shutdown(self) -> CanonicalResponse | None:
        return self._backend.shutdown()

    @property
    def is_shutting_down(self) -> bool:
        return self._backend.is_shutting_down

    def powerdown(self):
        if not self._backend.was_shut_down:
            self._backend.shutdown()
        while self._backend.is_shutting_down:
            time.sleep(1)
        self.power_off()

    def endpoint_shutdown(self) -> CanonicalResponse | None:
        return self.shutdown()

    @property
    def name(self) -> str:
        """
        The getter method for the imager's name.
        :return: The name of the imager
        """
        return self._backend.name

    @property
    def connected(self) -> bool:
        """
        Check if the imager is connected.
        :return: True if connected, False otherwise
        """
        return self._backend.connected

    @connected.setter
    def connected(self, value: bool):
        """
        Connect to the imager.
        This method should be called before any other methods that require a connection.
        :param value: True to connect, False to disconnect
        """
        self._backend.connected = value

    @property
    def camera_x_size(self) -> int | None:
        """
        Get the camera's X size in pixels.
        :return: The X size of the camera
        """
        return self._backend.camera_x_size

    @property
    def camera_y_size(self) -> int | None:
        """
        Get the camera's Y size in pixels.
        :return: The Y size of the camera
        """
        return self._backend.camera_y_size

    @property
    def cooler_power(self) -> float | None:
        """
        Get the current power of the camera cooler.
        :return: The cooler power in watts
        """
        return self._backend.cooler_power

    @property
    def operational(self) -> bool:
        """
        Check if the imager is operational.
        :return: True if the imager is operational, False otherwise
        """
        return self._backend.operational

    @property
    def why_not_operational(self) -> list[str]:
        """
        Get the reason why the imager is not operational.
        :return: A string explaining why the imager is not operational, or None if it is operational
        """
        return self._backend.why_not_operational

    def endpoint_abort(self) -> CanonicalResponse:
        return self.abort()

    def abort(self) -> CanonicalResponse:
        """
        Immediately terminates any in-progress activities and returns the imager to its default state.
        :return: CanonicalResponse indicating the result of the operation
        """
        return self._backend.endpoint_abort()

    def endpoint_status(self):
        return self.status()

    def status(self) -> ImagerStatus:
        """
        Returns the imager's current status.
        :return: ImagerStatus object containing the status information
        """
        backend_status = self._backend.status(capacity="imager")  # type: ignore

        ret = ImagerStatus(
            # detected=self.detected,
            connected=self.connected,
            # operational=self.operational,
            # why_not_operational=self.why_not_operational,
            camera_x_size=self.camera_x_size,
            camera_y_size=self.camera_y_size,
            temperature=self.temperature,
            powered=self.is_on(),
            cooler_on=self.cooler_on,
            cooler_power=self.cooler_power,
            set_point=self._backend.set_point,
            latest_settings=self.latest_settings,
            activities=self.activities,
            activities_verbal=self.activities_verbal,
            backend=backend_status if isinstance(backend_status, BaseModel) else backend_status.__dict__,
        )
        return ret

    def connect(self) -> CanonicalResponse | None:  # obsoleted by connected property
        self._backend.connected = True

    def disconnect(self) -> CanonicalResponse | None:  # obsoleted by connected property
        self.connected = False

    def start_exposure_series(self, purpose: str | None = None) -> ImagerExposureSeries:
        """
        An exposure series allows the imager backend to perform pre/post exposure activities.

        For example: the _`phd2`_ backend needs to stop/restart guiding if it was guiding when the series started
        """
        self.current_exposure_series = ImagerExposureSeries(purpose=purpose)
        logger.info(
            f"Starting exposure series id='{self.current_exposure_series.series_id}' "
            + f"purpose='{self.current_exposure_series.purpose}'"
        )
        return self.current_exposure_series

    def end_exposure_series(self, series: ImagerExposureSeries):
        """
        Ends the exposure series and cleans up resources.
        :param series: The ImagerExposureSeries to end
        """
        assert self.current_exposure_series is not None, "No current exposure series to end"
        if self.current_exposure_series.series_id != series.series_id:
            raise ValueError(
                f"Cannot end exposure series {series.series_id}, "
                + f"current series is {self.current_exposure_series.series_id}"
            )
        logger.info(f"Ending exposure series id='{series.series_id}', purpose='{series.purpose}'")
        self._backend.end_exposure_series(series)

    def start_exposure(self, settings: ImagerSettings) -> CanonicalResponse:
        """
        Starts an exposure with the given settings.
        :param settings: ImagerSettings object containing the exposure settings
        :return: CanonicalResponse indicating the result of the operation
        """
        self.latest_settings = settings
        return self._backend.start_exposure(settings)

    def stop_exposure(self) -> CanonicalResponse:
        """
        Stops the current exposure.
        :return: CanonicalResponse indicating the result of the operation
        """
        return self._backend.stop_exposure()

    def abort_exposure(self) -> CanonicalResponse:
        """
        Aborts the current exposure.
        :return: CanonicalResponse indicating the result of the operation
        """
        return self._backend.abort_exposure()

    @property
    def can_image_to_memory(self) -> bool:
        """
        Check if the imager can capture images to memory.
        """
        return self._backend.can_image_to_memory

    @property
    def image_array(self) -> np.ndarray | None:
        """
        Gets the image data from the imager.
        This method should be called after an exposure has been taken.
        :return: The image data as bytes
        """
        return self._backend.image_array if self._backend.can_image_to_memory else None

    @property
    def temperature(self) -> float | None:
        """
        Gets the current temperature of the camera.
        """
        return self._backend.temperature

    def wait_for_image_ready(self):
        """
        Waits for the image to be ready after an exposure.
        """
        if self.can_image_to_memory:
            self._backend.wait_for_image_ready()

    def wait_for_image_saved(self):
        """
        Waits for the image to be saved after an exposure.
        """
        self._backend.wait_for_image_saved()

    @property
    def cooler_on(self) -> bool | None:
        """
        Checks if the camera cooler is currently on.
        """
        return self._backend.cooler_on

    @cooler_on.setter
    def cooler_on(self, onoff: bool):
        """
        Turns the camera cooler on or off.
        :param onoff: True to turn on, False to turn off
        """
        self._backend.cooler_on = onoff

    @property
    def detected(self) -> bool:
        return self._backend.detected

    @property
    def was_shut_down(self) -> bool:
        return self._backend.was_shut_down

    @property
    def default_settings(self):
        return self._backend.default_settings

    @property
    def api_router(self) -> APIRouter:
        """
        Returns the API router for the imager.
        This is used to register the imager's API endpoints.
        """
        base_imager_path = Const.BASE_UNIT_PATH + "/imager"
        tag = "Imager"

        def cooler_on():
            self.cooler_on = True

        def cooler_off():
            self.cooler_on = False

        router = APIRouter()
        router.add_api_route(base_imager_path + "/startup", tags=[tag], endpoint=self.endpoint_startup)
        router.add_api_route(base_imager_path + "/shutdown", tags=[tag], endpoint=self.endpoint_shutdown)
        router.add_api_route(base_imager_path + "/abort", tags=[tag], endpoint=self.endpoint_abort)
        router.add_api_route(base_imager_path + "/status", tags=[tag], endpoint=self.endpoint_status)
        router.add_api_route(base_imager_path + "/connect", tags=[tag], endpoint=self.connect)
        router.add_api_route(base_imager_path + "/disconnect", tags=[tag], endpoint=self.disconnect)
        router.add_api_route(
            base_imager_path + "/start_exposure",
            tags=[tag],
            endpoint=self.start_exposure,
        )
        router.add_api_route(base_imager_path + "/stop_exposure", tags=[tag], endpoint=self.stop_exposure)
        router.add_api_route(
            base_imager_path + "/abort_exposure",
            tags=[tag],
            endpoint=self.abort_exposure,
        )
        router.add_api_route(base_imager_path + "/cooler_on", tags=[tag], endpoint=cooler_on)
        router.add_api_route(base_imager_path + "/cooler_off", tags=[tag], endpoint=cooler_off)

        return router
