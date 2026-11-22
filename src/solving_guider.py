import os
import threading
import time

import astropy.units as u
from astropy.coordinates import Angle

from common.activities import UnitActivities
from common.interfaces.guiding import GuiderInterface
from common.mast_logging import get_logger
from common.models.statuses import PHD2GuiderStatus
from common.utils import Coord, boxed_log
from solving import SolvingTolerance

logger = get_logger(__name__)

#: What `app_state` says while the loop runs and while it does not (PHD2's own words, so a reader of
#: `GuiderStatus.backend` sees the same vocabulary from either backend).
APP_STATE_GUIDING = "Guiding"
APP_STATE_STOPPED = "Stopped"


class SolvingGuider(GuiderInterface):
    """Guides by plate-solving: expose, solve, correct, wait out the rest of the cadence, repeat.

    Slow (a solve per correction) and the permanent fallback once PHD2 is retired
    (`plans/inhouse_guiding_design.md`, Stage 1).
    """

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
        #: Set by `stop_guiding`: wakes the loop out of its cadence wait, so a stop does not wait for the cycle to end.
        self._stop = threading.Event()
        self._initialized = True

    def start_guiding(self):
        phase = "guiding"
        boxed_log(logger, [f"starting phase '{phase.upper()}'"])

        assert self.unit.acquirer.latest_acquisition is not None, "self.unit.acquirer.latest_acquisition is None"

        phase_conf = self.unit.unit_conf.guiding
        cadence = phase_conf.cadence_seconds
        ra_tolerance = Angle(phase_conf.tolerance.ra_arcsec * u.arcsecond)  # type: ignore
        dec_tolerance = Angle(phase_conf.tolerance.dec_arcsec * u.arcsecond)  # type: ignore

        folder = os.path.join(self.unit.acquirer.latest_acquisition.folder, phase)
        guiding_settings = self.unit.guider.make_guiding_settings(folder)
        target: Coord = Coord(
            Angle(self.unit.acquirer.latest_acquisition.target_ra, unit="hourangle"),
            Angle(self.unit.acquirer.latest_acquisition.target_dec, unit="deg"),
        )

        self._stop.clear()
        self.guiding_exposure_series = self.unit.imager.start_exposure_series(purpose="guiding (solving)")
        self.unit.start_activity(UnitActivities.Guiding)
        try:
            while self.unit.is_active(UnitActivities.Guiding):
                start = time.monotonic()
                self.unit.solver.solve_and_correct(
                    target=target,
                    approach_mode=self.unit.acquirer.latest_acquisition.approach_mode,
                    solver_id=self.unit.acquirer.latest_acquisition.solver_id,
                    make_corrections=self.unit.acquirer.latest_acquisition.make_corrections,
                    imager_settings=guiding_settings,
                    solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                    # This loop's own activity. It was `Acquiring`, which is not active while guiding, so the
                    # solve counted itself cancelled the moment it started.
                    parent_activity=UnitActivities.Guiding,
                    phase=phase,
                )
                if cadence:
                    self._wait_out_cadence(start + cadence, cadence, phase)
        finally:
            if self.unit.acquirer.latest_acquisition is not None:
                self.unit.acquirer.latest_acquisition.save_corrections(phase)
            self._end_exposure_series()
            # Normally the loop ended because the activity did; if it ended on an exception the unit would look
            # busy guiding for the life of the process.
            if self.unit.is_active(UnitActivities.Guiding):
                self.unit.end_activity(UnitActivities.Guiding)

    def _wait_out_cadence(self, end: float, cadence: float, phase: str) -> None:
        """Sleep till ``end`` (a `time.monotonic` instant), or until `stop_guiding` wakes the wait.

        Monotonic, and a plain subtraction: ``timedelta.seconds`` is the 0-86399 component, so it truncated any
        sub-second remainder to zero.  This used to run once, after the loop had ended, so the loop free-ran.
        """
        remaining = end - time.monotonic()
        if remaining > 0:
            boxed_log(logger, f"phase '[{phase.upper()}], sleeping {remaining:.2f} seconds till end-of-cadence ...")
            self._stop.wait(remaining)
        else:
            boxed_log(logger, f"phase '[{phase.upper()}], cycle was longer than {cadence=} sec, not sleeping")

    def _end_exposure_series(self) -> None:
        """End the guiding series, once: the loop's end and `stop_guiding` both come here."""
        series, self.guiding_exposure_series = self.guiding_exposure_series, None
        if series is not None:
            self.unit.imager.end_exposure_series(series)

    def stop_guiding(self):
        self._stop.set()
        self._end_exposure_series()
        self.unit.end_activity(UnitActivities.Guiding)

    def guider_status(self) -> PHD2GuiderStatus:
        """For `Guider.status()`, which asks every backend for this. It was missing, so the status of a unit whose
        guider is this one raised AttributeError. The model is PHD2's, as `GuiderStatus.backend` is typed; there is
        no star lock or settling here, and no running distance: a solve returns only whether it got within tolerance."""
        guiding = self.is_guiding
        return PHD2GuiderStatus(
            identifier="solving",
            is_guiding=guiding,
            is_settling=False,
            app_state=APP_STATE_GUIDING if guiding else APP_STATE_STOPPED,
        )

    def status(self) -> PHD2GuiderStatus:
        return self.guider_status()

    @property
    def is_guiding(self) -> bool:
        return self.unit.is_active(UnitActivities.Guiding)
