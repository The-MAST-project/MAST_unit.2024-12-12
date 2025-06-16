import datetime
from abc import ABC, abstractmethod
from math import e
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from fastapi import APIRouter
from pydantic import BaseModel, Field

from common.ascom import AscomStatus
from common.canonical import CanonicalResponse
from common.components import Component, ComponentStatus

if TYPE_CHECKING:
    from unit import Unit

from common.const import Const
from common.dlipowerswitch import PowerStatus
from common.paths import PathMaker

__all__ = ["ImagerInterface", "ImagerSettings", "ImagerBinning", "ImagerRoi", "ImagerExposure", "ImagerStatus"]

class ImagerBinning(BaseModel):
    x: int = 1
    y: int = 1

    def __str__(self):
        return f"{self.x}x{self.y}"

class ImagerRoi(BaseModel):
    """
    Lower left corner of the ROI, and its width and height.
    """
    x: int = 0
    y: int = 0
    width: int = 1000
    height: int = 1000

    def __str__(self):
        return f"{self.x},{self.y},{self.width},{self.height}"

    @staticmethod
    def from_other(binning: ImagerBinning, other):
        """
        An imager ROI has a starting pixel (x, y) at lower left corner, width and height
        """
        if not binning:
            binning = ImagerBinning(x=1, y=1)

        if other.width is None or other.height is None:
            raise ValueError(f"ImagerRoi.from_other(): width or height is None in {other}")

        if hasattr(other, "sky_x") and hasattr(other, "sky_y"):
            center_x = other.sky_x
            center_y = other.sky_y
        elif hasattr(other, "fiber_x") and hasattr(other, "fiber_y"):
            center_x = other.fiber_x
            center_y = other.fiber_y
        elif hasattr(other, "center_x") and hasattr(other, "center_y"):
            center_x = other.center_x
            center_y = other.center_y
        else:
            raise ValueError(f"ImagerRoi.from_other(): unknown type {type(other)}")

        return ImagerRoi(
            x=(center_x - int(other.width / 2)) * binning.x,
            y=(center_y - int(other.height / 2)) * binning.y,
            width=other.width * binning.x,
            height=other.height * binning.y,
        )

class ImagerSettings(BaseModel):
    """
    Multipurpose exposure context

    Callers to start_exposure() fill in:
    - seconds - duration in seconds
    - base_folder - [optional] supplied folder under which the new folder/file will reside
    - gain - to be applied to the self by start_exposure()
    - binning - ditto
    - roi - ditto
    - tags - a flat dictionary of tags, will be added to the file name as ',name=value' or
       just ',name' if the value is None
    - save - whether to save to file or just keep in memory
    - fits_cards - to be added to the default ones
    """
    seconds: float
    base_folder: str | None = None
    image_path: str | None = None
    binning: ImagerBinning | None = ImagerBinning(x=1, y=1)
    gain: int | None = None
    roi: ImagerRoi | None = None
    tags: dict | None = {}
    save: bool = True
    fits_cards: dict[str, tuple] | None = {}
    start: datetime.datetime = Field(default=datetime.datetime.now(), exclude=True)
    file_name_parts: list[str] = Field(default=[], exclude=True)
    folder: str | None = Field(default=None, exclude=True)

    def model_post_init(self, __context):
        if self.save:
            if self.image_path is not None:
                folder = Path(self.image_path).parent
                self.folder = str(folder)
                folder.mkdir(parents=True, exist_ok=True)
            elif self.base_folder is not None:
                folder = Path(self.base_folder)
                folder.mkdir(parents=True, exist_ok=True)
                self.folder = str(folder)
                self.make_file_name()
            else:
                raise ValueError(
                    "ImagerSettings: either 'image_path' or 'base_folder' MUST be supplied when save=True"
                )

    def make_file_name(self, additional_tags: dict | None = None):
        """
        Makes the file part of the image path.  This will:
        - generate current seq= and time= file name parts
        - prepend optional additional_tags to those passed to the constructor

        :param additional_tags: tags specific to THIS making of the file name
        :return:
        """
        if not self.folder:
            raise ValueError("ImagerSettings: 'folder' must be set before making file name")

        self.file_name_parts = []
        self.file_name_parts.append(
            f"seq={PathMaker().make_seq(self.folder, start_with=-1)}"
        )
        self.file_name_parts.append(f"time={PathMaker().current_utc()}")

        tags = {}
        if additional_tags:
            tags = additional_tags
        if self.tags:
            tags.update(self.tags)
        for k, v in tags.items():
            self.file_name_parts.append(f"{k}" if v is None else f"{k}={v}")

        self.file_name_parts.append(f"seconds={self.seconds}")
        self.file_name_parts.append(f"binning={self.binning}")
        self.file_name_parts.append(f"gain={self.gain}")
        self.file_name_parts.append(f"roi={self.roi}")

        self.image_path = str(Path(self.folder, ",".join(self.file_name_parts) + ".fits"))

class ImagerExposure(BaseModel):
    file: str | None = None
    seconds: float | None = None
    date: datetime.datetime | None = None


class ImagerStatus(PowerStatus, AscomStatus, ComponentStatus):
    errors: list[str] | None = None
    set_point: float | None = None
    temperature: float | None = None
    cooler: bool = False
    cooler_power: float | None = None
    latest_exposure: ImagerExposure | None = None
    date: str | None = None


class ImagerInterface(Component, ABC):

    @property
    @abstractmethod
    def connected(self) -> bool:
        """
        Check if the imager is connected.
        :return: True if connected, False otherwise
        """
        pass

    @connected.setter
    @abstractmethod
    def connected(self, value: bool):
        """
        Connect to the imager.
        This method should be called before any other methods that require a connection.
        """
        pass

    @property
    @abstractmethod
    def camera_x_size(self) -> int | None:
        """
        Get the camera's X size in pixels.
        """
        pass

    @property
    @abstractmethod
    def camera_y_size(self) -> int | None:
        """
        Get the camera's Y size in pixels.
        """
        pass

    @abstractmethod
    def start_exposure(self, settings: ImagerSettings):
        self.latest_settings = settings
        pass

    @abstractmethod
    def stop_exposure(self):
        pass

    @property
    @abstractmethod
    def can_image_to_memory(self) -> bool:
        """
        Check if the imager can capture images to memory.
        :return: True if the imager can capture images to memory, False otherwise
        """
        pass

    @abstractmethod
    def abort_exposure(self):
        pass

    @abstractmethod
    def wait_for_image_ready(self):
        pass

    @abstractmethod
    def wait_for_image_saved(self):
        pass

    @property
    @abstractmethod
    def temperature(self) -> float:
        pass

    @property
    @abstractmethod
    def cooler_on(self) -> bool:
        """
        Check if the camera cooler is currently on.
        :return: True if the cooler is on, False otherwise
        """
        pass

    @cooler_on.setter
    @abstractmethod
    def cooler_on(self, onoff: bool):
        pass

    @property
    @abstractmethod
    def cooler_power(self) -> float | None:
        pass

    @property
    @abstractmethod
    def image_array(self) -> np.ndarray | None:
        """
        Get the image data from the imager.
        This method should be called after an exposure has been taken.
        """
        pass

    @property
    def full_frame(self) -> ImagerRoi:
        """
        Get the full frame ROI of the imager.
        """
        if self.camera_x_size is None or self.camera_y_size is None:
            raise ValueError("Camera X and Y sizes must be set before getting full frame ROI")

        return ImagerRoi(x=0, y=0, width=self.camera_x_size, height=self.camera_y_size)


class Imager(ImagerInterface):
    """
    This is the base class for all imagers.
    It provides the common interface and some common functionality.
    """
    _instance = None
    _initialized = False


    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, unit: "Unit", imager_params: dict | None = None):
        """
        Initializes the backend of an Imager instance according to the unit configuration.

        :param unit: "Unit" instance
        :param imager_params: Parameters for the imager
        """
        if self._initialized:
            return  # already initialized, do not re-initialize

        super().__init__()
        self.unit = unit
        self.conf = self.unit.unit_conf.imager

        imager_type = self.conf.imager_type.lower()
        if imager_type.startswith("ascom:"):
            from imagers.ascom import ASCOMImager
            self._backend = ASCOMImager(unit=unit, prog_id=self.conf.imager_type[6:])
        elif imager_type == "phd2":
            from phd2.phd2 import PHD2Connector
            self._backend = PHD2Connector(unit=unit)
        # elif type == "zwo":
        #     from imagers.zwo.zwo_imager import ZWOImager
        #     self._backend = ZWOImager(unit=unit, imager_params=imager_params)
        else:
            raise ValueError(f"Unknown imager type: {self.conf.imager_type}")

        self.imager_params = imager_params
        self.latest_settings: ImagerSettings | None = None
        self._initialized = True

    def startup(self) -> CanonicalResponse | None:
        return self._backend.startup()

    def shutdown(self) -> CanonicalResponse | None:
        return self._backend.shutdown()

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

    def abort(self) -> CanonicalResponse | None:
        """
        Immediately terminates any in-progress activities and returns the imager to its default state.
        :return: CanonicalResponse indicating the result of the operation
        """
        return self._backend.abort()

    def status(self):
        """
        Returns the imager's current status.
        :return: ImagerStatus object containing the status information
        """
        return self._backend.status()

    def connect(self) -> CanonicalResponse | None: # obsoleted by connected property
        self._backend.connected = True

    def disconnect(self) -> CanonicalResponse | None: # obsoleted by connected property
        self.connected = False

    def start_exposure(self, settings: ImagerSettings) -> CanonicalResponse | None:
        """
        Starts an exposure with the given settings.
        :param settings: ImagerSettings object containing the exposure settings
        :return: CanonicalResponse indicating the result of the operation
        """
        return self._backend.start_exposure(settings)

    def stop_exposure(self) -> CanonicalResponse | None:
        """
        Stops the current exposure.
        :return: CanonicalResponse indicating the result of the operation
        """
        return self._backend.stop_exposure()

    def abort_exposure(self) -> CanonicalResponse | None:
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

    def temperature(self) -> float:
        """
        Gets the current temperature of the camera.
        """
        return self._backend.temperature

    def wait_for_image_ready(self) -> CanonicalResponse | None:
        """
        Waits for the image to be ready after an exposure.
        """
        if self.can_image_to_memory:
            return self._backend.wait_for_image_ready()

    def wait_for_image_saved(self) -> CanonicalResponse | None:
        """
        Waits for the image to be saved after an exposure.
        """
        return self._backend.wait_for_image_saved()

    @property
    def cooler_on(self) -> bool:
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
        router.add_api_route(base_imager_path + "/startup", tags=[tag], endpoint=self.startup)
        router.add_api_route(
            base_imager_path + "/shutdown", tags=[tag], endpoint=self.shutdown
        )
        router.add_api_route(base_imager_path + "/abort", tags=[tag], endpoint=self.abort)
        router.add_api_route(base_imager_path + "/status", tags=[tag], endpoint=self.status)
        router.add_api_route(base_imager_path + "/connect", tags=[tag], endpoint=self.connect)
        router.add_api_route(base_imager_path + "/disconnect", tags=[tag], endpoint=self.disconnect)
        router.add_api_route(base_imager_path + "/start_exposure", tags=[tag], endpoint=self.start_exposure)
        router.add_api_route(base_imager_path + "/stop_exposure", tags=[tag], endpoint=self.stop_exposure)
        router.add_api_route(base_imager_path + "/abort_exposure", tags=[tag], endpoint=self.abort_exposure)
        router.add_api_route(base_imager_path + "/cooler_on", tags=[tag], endpoint=cooler_on)
        router.add_api_route(base_imager_path + "/cooler_off", tags=[tag], endpoint=cooler_off)

        return router
