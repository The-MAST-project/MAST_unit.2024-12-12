import datetime
import os
import time
from logging import Logger
from typing import TYPE_CHECKING

import astropy.units as u
from astropy.coordinates import Angle

from common.activities import UnitActivities
from common.interfaces.guiding import GuiderInterface
from common.mast_logging import init_log
from common.utils import Coord, boxed_log
from solving import SolvingTolerance

logger = Logger("mast-unit-solving-guider")
init_log(logger)

if TYPE_CHECKING:
    pass


class SolvingGuider(GuiderInterface):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, unit):
        if self._initialized:
            return

        if unit is None:
            raise ValueError(" unit is None")

        self.unit = unit
        self.guiding_exposure_series = None
        self._initialized = True

    def start_guiding(self):
        phase = "guiding"
        boxed_log(logger, [f"starting phase '{phase.upper()}'"])

        assert (
            self.unit.acquirer.latest_acquisition is not None
        ), "self.unit.acquirer.latest_acquisition is None"

        phase_conf = self.unit.unit_conf.guiding
        cadence = phase_conf.cadence_seconds
        ra_tolerance = Angle(phase_conf.tolerance.ra_arcsec * u.arcsecond)  # type: ignore
        dec_tolerance = Angle(phase_conf.tolerance.dec_arcsec * u.arcsecond)  # type: ignore

        end: datetime.datetime | None = None
        folder = os.path.join(self.unit.acquirer.latest_acquisition.folder, phase)
        guiding_settings = self.unit.guider.make_guiding_settings(folder)
        target: Coord = Coord(
            Angle(self.unit.acquirer.latest_acquisition.target_ra, unit="hourangle"),
            Angle(self.unit.acquirer.latest_acquisition.target_dec, unit="deg"),
        )

        self.guiding_exposure_series = self.unit.imager.start_exposure_series()
        self.unit.start_activity(UnitActivities.Guiding)
        while self.unit.is_active(UnitActivities.Guiding):
            start = datetime.datetime.now()
            if cadence:
                end = start + datetime.timedelta(seconds=cadence)
            self.unit.solver.solve_and_correct(
                target=target,
                approach_mode=self.unit.acquirer.latest_acquisition.approach_mode,
                solver_id=self.unit.acquirer.latest_acquisition.solver_id,
                make_corrections=self.unit.acquirer.latest_acquisition.make_corrections,
                imager_settings=guiding_settings,
                solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                parent_activity=UnitActivities.Acquiring,
                phase=phase,
            )

        if self.unit.acquirer.latest_acquisition is not None:
            self.unit.acquirer.latest_acquisition.save_corrections(phase)

        if cadence and end is not None:
            now = datetime.datetime.now()
            if now < end:
                sec = (end - now).seconds
                boxed_log(
                    logger,
                    f"phase '[{phase.upper()}], sleeping {sec:.2f} seconds till end-of-cadence ...",
                )
                time.sleep(sec)
            else:
                boxed_log(
                    logger,
                    f"phase '[{phase.upper()}], cycle was longer than {cadence=} sec, not sleeping",
                )

    def stop_guiding(self):
        self.unit.imager.end_exposure_series(self.guiding_exposure_series)
        self.guiding_exposure_series = None
        self.unit.end_activity(UnitActivities.Guiding)

    def status(self):
        pass

    @property
    def is_guiding(self) -> bool:
        return self.unit.is_active(UnitActivities.Guiding)
