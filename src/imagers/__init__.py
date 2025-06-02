import datetime
from abc import ABC, abstractmethod
from enum import Enum, auto
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from common.ascom import AscomStatus
from common.components import Component, ComponentStatus
from common.const import Const
from common.dlipowerswitch import PowerStatus
from common.paths import PathMaker
from src.common.canonical import CanonicalResponse

__all__ = ["ImagerInterface", "ImagerType", "ImagerSettings", "ImagerBinning", "ImagerRoi", "ImagerExposure", "ImagerStatus"]

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


class ImagerSettings:
    """

    Multipurpose self exposure context

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

    After start_exposure() is called:
    - image_path - contains the full path to the saved file, with a standard combination of the context elements
               <folder>/seq=<sequence>,tags=<tag1=value1,tag2,tag3=value3>,binning=<binning>,gain=<gain>,roi=<roi>
    - start - contains the exposure start time

    Note:
     start_exposure() copies these settings to imager.latest_settings thus making it available for further use

    """

    def __init__(
        self,
        seconds: float,
        gain: int | None = None,
        binning: ImagerBinning | None = None,
        roi: ImagerRoi | None = None,
        tags: dict | None = None,
        save: bool = True,
        fits_cards: dict[str, tuple] | None = None,
        base_folder: str | None = None,
        image_path: str | None = None,
    ):

        self.seconds: float = seconds
        self.base_folder: str | None = base_folder
        self.image_path: str | None = image_path
        self.binning: ImagerBinning | None = binning
        self.gain: int | None = gain
        self.roi: ImagerRoi | None = roi
        self.tags: dict | None = tags if tags else {}
        self.save: bool = save
        self.fits_cards: dict[str, tuple] | None = fits_cards if fits_cards else {}
        self.start: datetime.datetime = datetime.datetime.now()
        self.file_name_parts: list[str] = []

        if self.save:
            if self.image_path is not None:
                path = Path(self.image_path)
                path.parent.mkdir(parents=True, exist_ok=True)
            elif self.base_folder is not None:
                self.folder = self.base_folder
                path = Path(self.folder)
                path.mkdir(parents=True, exist_ok=True)
                self.make_file_name()
            else:
                raise Exception(
                    "CameraSettings:__init__(): either 'image_path' or 'base_folder' MUST be supplied"
                )

    def make_file_name(self, additional_tags: dict | None = None):
        """
        Makes the file part of the image path.  This will:
        - generate current seq= and time= file name parts
        - prepend optional additional_tags to those passed to the constructor

        :param additional_tags: tags specific to THIS making of the file name
        :return:
        """
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


class ImagerType(Enum):
    Ascom = auto()
    ZWO = auto()
    PHD2 = auto()


class ImagerConf(BaseModel):
    type: str


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

    from unit import Unit
    def __init__(self, unit: Unit, imager_params: dict | None = None):
        """
        Initializes the backend of an Imager instance according to the unit configuration.

        :param unit: Unit instance
        :param imager_params: Parameters for the imager
        """
        if self._initialized:
            return  # already initialized, do not re-initialize

        super().__init__()
        self.unit = unit
        self.conf: ImagerConf = ImagerConf(**self.unit.unit_conf["imager"])

        type = self.conf.type.lower
        if type.startswith("ascom:"):
            from imagers.ascom import ASCOMImager
            self._backend = ASCOMImager(unit=unit, prog_id=self.conf.type[6:])
        elif type == "phd2":
            from imagers.phd2 import PHD2Imager
            self._backend = PHD2Imager(unit=unit)
        # elif type == "zwo":
        #     from imagers.zwo import ZWOImager
        #     self._backend = ZWOImager(unit=unit, imager_params=imager_params)
        else:
            raise ValueError(f"Unknown imager type: {self.conf.type}")

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

    def status(self) -> ImagerStatus | None:
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
        router.add_api_route(
            base_imager_path + "/disconnect", tags=[tag], endpoint=self.disconnect
        )
        router.add_api_route(
            base_imager_path + "/start_exposure",
            tags=[tag],
            endpoint=self.start_exposure,
        )
        router.add_api_route(
            base_imager_path + "/stop_exposure", tags=[tag], endpoint=self.stop_exposure
        )
        router.add_api_route(
            base_imager_path + "/abort_exposure", tags=[tag], endpoint=self.abort_exposure
        )
        router.add_api_route(
            base_imager_path + "/cooler_on", tags=[tag], endpoint=cooler_on
        )
        router.add_api_route(
            base_imager_path + "/cooler_off", tags=[tag], endpoint=cooler_off
        )

        return router
