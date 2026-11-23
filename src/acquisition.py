import datetime
import os
import time
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from common import asi
from common.corrections import Corrections
from common.filer import Filer, FilingArea, MoveGuardian
from common.interfaces.solving import SolverId
from common.mast_logging import get_logger
from common.models.latest import LatestAcquisition
from common.paths import PathMaker
from imaging.mirror_shadow import ShadowModel, StagedShadow, detect_mirror_shadow
from imaging.shadow_store import ShadowStore, StoredShadow, frame_shape, within
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
            st = self.unit.required_mount.status()
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
        #: Set by `Acquirer.run_acquisition` when the acquisition ends: None when it
        #: reached tolerance, otherwise why it did not. `Acquiring` clears either way.
        self.failure: str | None = None
        #: Set by `Acquirer.start`: what `FullUnitStatus.latest.acquisition` reports for this run.
        self.latest: LatestAcquisition | None = None
        #: The fold-mirror shadow, detected on a full frame and kept with the phase and the stage position it
        #: was found at (`shadow_for`). None until then. Detection takes seconds on a full frame, and the band
        #: only moves when the stage does.
        self.shadow: StagedShadow | None = None
        #: The machine's shadow models on the share (`imaging/shadow_store.py`), under the one in `shadow`: a model detected
        #: by an earlier acquisition, another process or the guider is found there. None: there is no such folder.
        self.shadow_store: ShadowStore | None = ShadowStore.for_machine()

    def shadow_for(self, image_path: str, phase: str | None = None) -> ShadowModel | None:
        """The shadow model of the full frame `image_path`: the cached one while the phase and the stage are
        where it was detected, else a new detection, which replaces it.

        None when detection itself fails; nothing is cached then, so the next frame tries again. A model with
        `present` False (no band found) is cached like any other: it is a result. The shadow is looked for in
        every phase, none being assumed from the stage position or the phase name.
        """
        stage = getattr(self.unit, "stage", None)
        position = getattr(stage, "position", None) if stage is not None else None
        stage_conf = getattr(stage, "conf", None) if stage is not None else None
        close_enough = getattr(stage_conf, "close_enough", 0) if stage_conf is not None else 0

        cached = self.shadow
        if cached is not None and cached.matches(position, close_enough, phase):
            return cached.model
        if cached is not None:
            logger.info(
                f"the shadow was detected in phase {cached.phase} at stage position {cached.stage_position}; "
                f"now {phase} at {position}: detecting again"
            )
        store = getattr(self, "shadow_store", None)
        # whether a stored position is the stage's: the stage's own `close_enough`
        is_close = getattr(stage, "close_enough", None)
        if not callable(is_close):
            is_close = within(position, close_enough)
        if store is not None and position is not None:
            stored = store.find(position, is_close, frame_shape(image_path))
            if stored is not None:
                logger.info(
                    f"shadow model from the store ({phase}, stage position {position}, detected {stored.detected_at})"
                )
                self.shadow = StagedShadow(
                    model=stored.model,
                    stage_position=position,
                    image_path=stored.image_path,
                    detected_at=stored.detected_at,
                    seconds=0.0,
                    phase=phase,
                )
                return stored.model
        started = time.monotonic()
        try:
            model = detect_mirror_shadow(image_path)
        except Exception as e:  # noqa: BLE001 -- a shadow we cannot find must not cost the solve
            logger.warning(f"shadow detection failed on '{image_path}' ({e!r}); the solver will detect it itself")
            return None
        seconds = time.monotonic() - started
        self.shadow = StagedShadow(
            model=model,
            stage_position=position,
            image_path=str(image_path),
            detected_at=datetime.datetime.now(datetime.UTC).isoformat(),
            seconds=seconds,
            phase=phase,
        )
        logger.info(f"shadow detected in {seconds:.1f} s ({phase}, stage position {position}): present={model.present}")
        shape = frame_shape(image_path)
        if store is not None and position is not None and model.present and shape is not None:
            store.save(
                StoredShadow(
                    model=model,
                    stage_position=int(position),
                    detected_at=self.shadow.detected_at,
                    seconds=seconds,
                    image_path=str(image_path),
                    phase=phase,
                ),
                is_close,
            )
        return model

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
            plot_acquisition_corrections(self._folder_on_the_share())

    def _folder_on_the_share(self) -> str:
        """This acquisition's folder, as moved to the shared area.

        Through `Filer.to_top`, not `self.folder.replace(ram.root, shared.root)`: the roots
        are stored forward-slashed ("D:/MAST/") and PathMaker built folders with backslashes
        on Windows, so the replace never matched there and the plot was always pointed at
        the ram-disk folder (plans/filing-areas-design.md 1.4). A folder that is not on the
        ram area is returned unchanged, which is what the replace did, but now only then.
        """
        try:
            area, _ = filer.area_of(self.folder)
        except ValueError:
            return self.folder
        if area is not FilingArea.RAM:
            return self.folder
        return filer.to_top(FilingArea.SHARE, self.folder)
