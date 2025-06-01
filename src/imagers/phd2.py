from . import ImagerInterface


class PHD2Imager(ImagerInterface):
    """
    PHD2Imager is a class that implements the ImagerInterface for PHD2.
    It provides methods to interact with the PHD2 imaging software.
    """

    def __init__(self, unit, imager_params=None):
        super().__init__()
        self.unit = unit
        self.imager_params = imager_params or {}
        # Initialize PHD2 connection here if needed

    def capture(self):
        # Implement capture logic for PHD2
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
