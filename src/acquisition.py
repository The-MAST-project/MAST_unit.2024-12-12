import datetime
import logging
import os
from enum import IntEnum
from typing import TYPE_CHECKING, Any

import common.asi as asi
from common.config.unit import AcquisitionConfig
from common.corrections import Corrections
from common.filer import Filer
from common.mast_logging import init_log
from common.paths import PathMaker
from common.transfer import TransferTracker
from common.solving import SolverId
from plotting import plot_acquisition_corrections, plot_phase_corrections

if TYPE_CHECKING:
    from unit import Unit

logger = logging.getLogger("mast.unit." + __name__)
filer = Filer(logger)
init_log(logger)


class ApproachMode(IntEnum):
    """
    How `solve_and_correct` applies a mount correction. IntEnum, so existing
    integer values (config, API query params, stored acquisitions) remain valid
    and `match`/`==` still work against a plain int subject.
    """

    DISCRETE_STEP = 1  # mount_offset(ra/dec_add_arcsec=…) — single discrete jump
    GRADUAL_BY_RATE = 2  # add_gradual_offset_arcsec + gradual_offset_rate
    GRADUAL_BY_TIME = 3  # add_gradual_offset_arcsec + gradual_offset_seconds (resets first)
    STEP_WITH_TRACKING_RATE = 4  # add_arcsec + set_rate_arcsec_per_sec


class Acquisition:

    def __init__(
        self,
        unit: "Unit",
        approach_mode: ApproachMode,
        solver_id: SolverId,
        make_corrections: bool = True,
        target_ra: float | None = None,
        target_dec: float | None = None,
        conf: AcquisitionConfig | None = None,
        skip_sky: bool = False,
        use_set_limit_frame: bool = True,
        handover_automatically_to_guider: bool = False,
        gain_absolute: int | None = None,
        gain_percent: int | None = None,
    ):
        if not conf:
            raise Exception("Acquisition: conf == None")

        self.approach_mode = approach_mode
        self.solver_id = solver_id
        self.make_corrections = make_corrections
        self.handover_automatically_to_guider = handover_automatically_to_guider
        self.unit = unit
        self.gain_absolute = gain_absolute or asi.ASI_294MM_DEFAULT_GAIN
        self.gain_percent = gain_percent
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
        # Tag this acquisition's transfers so the tracker can reconcile/await them
        # as a group (see post_process). Observability only, not a source of truth.
        self._transfer_tag = os.path.basename(self.folder)
        self.skip_sky = skip_sky
        self.use_set_limit_frame = use_set_limit_frame
        self.solver_data: Any = None  # May be set by the solver, to remember something

    def save_corrections(self, phase: str):
        if phase in self.corrections:
            path = os.path.join(self.folder, phase, "corrections.json")
            for _ in range(3):
                try:
                    with filer.atomic_path(path, tag=self._transfer_tag) as tmp:
                        with open(tmp, "w") as fp:
                            fp.write(self.corrections[phase].model_dump_json(indent=2))
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
            filer.move_ram_to_shared([path, path.replace("json", "png")], tag=self._transfer_tag)

    def post_process(self):
        if filer.ram and filer.ram.root is not None:
            # Await this acquisition's products being persisted to the shared store
            # (and log a per-sequence reconciliation) instead of racing the move,
            # then plot from there. The filesystem stays the truth -- plotting reads
            # the shared copy regardless of the tracker.
            TransferTracker.instance().wait_for_tag(self._transfer_tag, timeout=60)
            plot_acquisition_corrections(
                self.folder.replace(filer.ram.root, filer.shared.root)
            )
