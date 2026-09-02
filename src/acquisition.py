import datetime
import os
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from common import asi
from common.corrections import Corrections
from common.filer import Filer, MoveGuardian
from common.mast_logging import get_logger
from common.paths import PathMaker
from common.solving import SolverId
from plotting import plot_acquisition_corrections, plot_phase_corrections

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)
filer = Filer(logger)


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
        exposure: float | None = None,
        skip_sky: bool = False,
        use_set_limit_frame: bool = True,
        handover_automatically_to_guider: bool = False,
        gain_absolute: int | None = None,
        gain_percent: int | None = None,
    ):
        #: A caller-supplied exposure for THIS acquisition only, or None to use the
        #: configured one. It used to arrive as a whole `AcquisitionConfig`, which the
        #: endpoint produced by assigning into `unit.unit_conf.acquisition.exposure` --
        #: a write into the process-wide configuration every other component reads, never
        #: persisted and never reset. A plan-driven acquisition then inherited whatever
        #: exposure an operator had last typed into the HTTP endpoint (MAST_unit#195).
        #:
        #: Passing the config object at all was redundant: both call sites passed
        #: `self.unit.unit_conf.acquisition`, which `do_acquire` re-reads directly anyway.
        #: Its only distinguishing content was this override, so the override is now the
        #: only thing passed.
        self.exposure = exposure

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
                raise ValueError("Acquisition: target_ra is None and mount status does not provide RA")
            if st.dec_j2000_degs is not None:
                self.target_dec = st.dec_j2000_degs
            else:
                raise ValueError("Acquisition: target_dec is None and mount status does not provide DEC")

        # `ra_tolerance` / `dec_tolerance` used to be copied out of the config here and
        # were read by nothing: every tolerance actually applied is derived where it is
        # used (`acquirer.py`, `solving_guider.py`) from the live configuration.
        self.corrections: dict[str, Corrections] = {}
        self.folder = PathMaker().make_acquisition_folder(
            tags={
                "target": f"{target_ra},{target_dec}",
            }
        )
        self.skip_sky = skip_sky
        self.use_set_limit_frame = use_set_limit_frame
        self.solver_data: Any = None  # May be set by the solver, to remember something

    def save_corrections(self, phase: str):
        if phase in self.corrections:
            path = os.path.join(self.folder, phase, "corrections.json")
            png = path.replace("json", "png")
            # Protect both artifacts until they are fully written AND handed to the mover,
            # so the ram->shared move can't copy a half-written file (replaces sleep(2)).
            with MoveGuardian().protect(path, png):
                for _ in range(3):
                    try:
                        with open(path, "w") as fp:
                            fp.write(self.corrections[phase].model_dump_json(indent=2))
                            break
                    except OSError as e:
                        logger.error(f"failed to write {path} (error: {e})")
                        continue
                plot_phase_corrections(
                    phase=phase,
                    corrections=self.corrections[phase],
                    file=path,
                    ends_of_phases=[datetime.datetime.now(datetime.UTC)],
                )
                filer.move_ram_to_shared(path)
                filer.move_ram_to_shared(png)

    def post_process(self):
        # NB: the ram-disk folder is NOT released here. post_process() is only reached by
        # acquisitions that ran to completion, and the ones worth clearing are the ones that
        # did not. Acquirer.run_acquisition owns the release, in a finally.
        if filer.ram and filer.ram.root is not None:
            plot_acquisition_corrections(self.folder.replace(filer.ram.root, filer.shared.root))
