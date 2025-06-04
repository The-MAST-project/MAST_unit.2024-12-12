from abc import ABC, abstractmethod

from common.activities import Activities


class GuiderInterface(ABC, Activities):

    @abstractmethod
    def start_guiding(self):
        """
        Starts guiding
        """
        pass

    @abstractmethod
    def stop_guiding(self):
        """Stops guiding"""
        pass

    @abstractmethod
    def status(self):
        pass

    @abstractmethod
    def is_guiding(self) -> bool:
        pass
