from camera import CameraSettings
from common.utils import Coord
from common.mast_logging import init_log
import logging
from solving import SolvingSolution, SolvingResult

logger = logging.Logger('astrometry_dot_net')
init_log(logger)


class AstrometryDotNetSolverResult:
    pass


def astrometry_dot_net_solve(unit: 'Unit', settings: CameraSettings, target: Coord) -> SolvingResult:
    pass
