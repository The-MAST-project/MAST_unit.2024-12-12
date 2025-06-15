import datetime
import json
import logging
import os
import time
from typing import TYPE_CHECKING

from common.config import AcquisitionConfig
from common.corrections import Corrections
from common.filer import Filer
from common.mast_logging import init_log
from common.paths import PathMaker
from common.solving import SolverId
from plotting import plot_acquisition_corrections, plot_phase_corrections

if TYPE_CHECKING:
    from unit import Unit

logger = logging.getLogger("mast.unit." + __name__)
filer = Filer(logger)
init_log(logger)


class Acquisition:

    from guiding import GuidingMode

    def __init__(
        self,
        unit: "Unit",
        approach_mode: int,
        solver_id: SolverId,
        make_corrections: bool = True,
        target_ra: float | None = None,
        target_dec: float | None = None,
        conf: AcquisitionConfig | None = None,
        skip_sky: bool = False,
        guiding_mode: GuidingMode = GuidingMode.PlateSolving,
    ):
        if not conf:
            raise Exception("Acquisition: conf == None")

        self.approach_mode = approach_mode
        self.solver_id = solver_id
        self.make_corrections = make_corrections
        self.unit = unit
        self.slew_to_target = False
        if target_ra is not None and target_dec is not None:
            self.target_ra: float = target_ra
            self.target_dec: float = target_dec
            self.slew_to_target = True
        else:
            st = self.unit.mount.status()
            if st.ra_j2000_hours is not None:
                self.target_ra = st.ra_j2000_hours
            else:
                raise ValueError(
                    "Acquisition: target_ra is None and mount status does not provide RA"
                )
            if st.dec_j2000_degs is not None:
                self.target_dec = st.dec_j2000_degs
            else:
                raise ValueError(
                    "Acquisition: target_dec is None and mount status does not provide DEC"
                )

        self.conf = conf
        self.ra_tolerance = conf.tolerance.ra_arcsec
        self.dec_tolerance = conf.tolerance.dec_arcsec
        self.corrections: dict[str, Corrections] = {}
        self.folder = PathMaker().make_acquisition_folder(
            tags={
                "target": f"{target_ra},{target_dec}",
            }
        )
        self.skip_sky = skip_sky
        self.guiding_mode = guiding_mode

    def save_corrections(self, phase: str):
        if phase in self.corrections:
            path = os.path.join(self.folder, phase, "corrections.json")
            for _ in range(3):
                try:
                    with open(path, "w") as fp:
                        json.dump((self.corrections[phase]).to_dict(), fp, indent=2)
                        break
                except Exception as e:
                    logger.error(f"failed to write {path} (error: {e})")
                    continue
            plot_phase_corrections(
                phase=phase,
                corrections=self.corrections[phase],
                file=path,
                ends_of_phases=[datetime.datetime.now(datetime.UTC)],
            )
            time.sleep(2)
            filer.move_ram_to_shared(path)
            filer.move_ram_to_shared(path.replace("json", "png"))

    def post_process(self):
        if filer.ram and filer.ram.root is not None:
            plot_acquisition_corrections(
                self.folder.replace(filer.ram.root, filer.shared.root)
            )
