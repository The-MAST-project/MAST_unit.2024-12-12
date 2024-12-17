import datetime
import logging
import time

from common.paths import PathMaker
from common.mast_logging import init_log
from common.corrections import Corrections
from common.solving import SolverId
from plotting import plot_acquisition_corrections, plot_phase_corrections
import os
import json
from common.filer import Filer
from typing import Dict, Optional

logger = logging.getLogger('mast.unit.' + __name__)
filer = Filer(logger)
init_log(logger)


class Acquisition:
    def __init__(self, unit: 'Unit',
                 approach_mode: int,
                 solver_id: SolverId,
                 make_corrections: bool = True,
                 target_ra: Optional[float] = None,
                 target_dec: Optional[float] = None,
                 conf: Optional[Dict] = None):
        if not conf:
            raise Exception(f"Acquisition: conf == None")

        self.approach_mode = approach_mode
        self.solver_id = solver_id
        self.make_corrections = make_corrections
        self.unit = unit
        self.slew_to_target = False
        if target_ra and target_dec:
            self.target_ra: float = target_ra
            self.target_dec: float = target_dec
            self.slew_to_target = True
        else:
            st = self.unit.mount.status()
            target_ra = st['ra_j2000_hours ']
            target_dec = st['dec_j2000_degs ']

        self.conf = conf
        self.ra_tolerance = conf['tolerance']['ra_arcsec']
        self.dec_tolerance = conf['tolerance']['dec_arcsec']
        self.corrections: Dict[str, Corrections] = {}
        self.folder = PathMaker().make_acquisition_folder(
            tags={
                'target': f"{target_ra},{target_dec}",
            })

    def save_corrections(self, phase: str):
        if phase in self.corrections:
            path = os.path.join(self.folder, phase, 'corrections.json')
            for i in range(3):
                try:
                    with open(path, 'w') as fp:
                        json.dump((self.corrections[phase]).to_dict(), fp, indent=2)
                        break
                except Exception as e:
                    logger.error(f"failed to write {path} (error: {e})")
                    continue
            plot_phase_corrections(phase=phase, corrections=self.corrections[phase], file=path,
                                   ends_of_phases=[datetime.datetime.now(datetime.UTC)])
            time.sleep(2)
            filer.move_ram_to_shared(path)
            filer.move_ram_to_shared(path.replace('json', 'png'))

    def post_process(self):
        plot_acquisition_corrections(self.folder.replace(filer.ram.root, filer.shared.root))