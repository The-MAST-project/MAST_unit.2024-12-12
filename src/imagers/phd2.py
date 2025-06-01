from . import ImagerInterface, ImagerSettings


class PHD2Imager(ImagerInterface):
    """
    PHD2Imager is a class that implements the ImagerInterface for PHD2.
    It provides methods to interact with the PHD2 imaging software.
    """

    def __init__(self, unit):
        super().__init__()
        self.unit = unit
        # Initialize PHD2 connection here if needed

    def startup(self):
        pass

    def shutdown(self):
        pass

    def abort(self):
        pass

    def connected(self) -> bool:
        return False

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

    def temperature(self) -> float:
        return 0.0

    def cooler(self, onoff: bool):
        pass

    def cooler_power(self) -> float:
        return 0.0

    @property
    def name(self) -> str:
        return "PHD2Imager"

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
