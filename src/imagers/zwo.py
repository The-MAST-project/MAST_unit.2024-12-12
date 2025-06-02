from . import ImagerInterface


class ZWOImager(ImagerInterface):
    """
    ZWOImager is a class that implements the ImagerInterface for ZWO cameras.
    It provides methods to interact with ZWO imaging software (ASI SDK).
    """

    def __init__(self, unit, imager_params=None):
        super().__init__()
        self.unit = unit
        self.imager_params = imager_params or {}
        # Initialize ZWO connection here if needed

    @property
    def can_image_to_memory(self) -> bool:
        return True

    def capture(self):
        # Implement capture logic for ZWO
        pass

    def wait_for_image_in_memory(self):
        # Implement logic to wait for image in memory
        pass

    def wait_for_image_saved(self):
        # Implement logic to wait for image to be saved
        pass

    def temperature(self) -> float:
        # Implement logic to get camera temperature
        return 0.0

    def cooler(self, onoff: bool):
        # Implement logic to turn cooler on/off
        pass

    def cooler_power(self) -> float:
        # Implement logic to get cooler power
        return 0.0
