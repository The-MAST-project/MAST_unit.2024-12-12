from abc import ABC, abstractmethod
from common.activities import Activities


class BaseGuider(ABC, Activities):

    @abstractmethod
    def start_guiding(self):
        """
        Starts guiding
        :return:
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
