from common.process import WatchedProcess
from imagers import ImagerInterface, ImagerSettings


class PHD2Imager(ImagerInterface):
    """
    PHD2Imager is a class that implements the ImagerInterface for PHD2.
    It provides methods to interact with the PHD2 imaging software.
    """
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, unit):
        super().__init__()
        self.unit = unit
        self._process = WatchedProcess(
            command="phd2.exe",
            env=None,
            cwd="C:/Program Files (x86)/PHDGuiding2",
        )
        self._initialized = True

    def __del__(self):
        if self._process:
            self._process.terminate()
            self._process = None

    @property
    def can_image_to_memory(self) -> bool:
        return False  # PHD2 does not support imaging to memory directly

    @property
    def camera_x_size(self) -> int:
        return 0

    @property
    def camera_y_size(self) -> int:
        return 0

    def startup(self):
        pass

    def shutdown(self):
        if self._process:
            self._process.terminate()
            self._process = None

    def abort(self):
        pass

    @property
    def connected(self) -> bool:
        return False

    @connected.setter
    def connected(self, value: bool):
        if value:
            self.connect()
        else:
            self.disconnect()

    def connect(self):
        pass

    def disconnect(self):
        pass

    def status(self):
        pass

    def start_exposure(self, settings: ImagerSettings):
        pass

    def stop_exposure(self):
        pass

    def abort_exposure(self):
        pass

    def wait_for_image_ready(self):
        pass

    def wait_for_image_saved(self):
        pass

    @property
    def temperature(self) -> float:
        return 0.0

    @property
    def cooler_on(self) -> bool:
        return False

    @cooler_on.setter
    def cooler_on(self, onoff: bool):
        pass

    @property
    def cooler_power(self) -> float:
        return 0.0

    @property
    def name(self) -> str:
        return "PHD2Imager"

    @property
    def image_array(self):
        return None

    @property
    def operational(self) -> bool:
        return True  # Assuming PHD2 is always operational when connected

    @property
    def why_not_operational(self) -> list[str]:
        return []  # No specific reasons for non-operational state in this implementation

    @property
    def was_shut_down(self) -> bool:
        return False  # Assuming PHD2 is not shut down in this implementation

    @property
    def detected(self) -> bool:
        return False
