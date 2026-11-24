"""The fold-mirror shadow models of this machine, kept in its local filing area.

``<local filing area>/Calibration/shadow_models.json`` (``C:/MAST/Calibration/shadow_models.json`` on a unit), one entry per
stage position and per binning. The file is the machine's own: a model is a measurement of this unit's mirror and stage, and
a unit that cannot reach the share still has it. (Until 2026-11-24 it was kept beside ``hot_pixels.json`` on the share, where
no model was ever written.)

* **Both binnings are kept.** An entry is keyed by the frame's shape, which says its binning, and records the binning. A
  model detected on a full frame (binning 1) is also saved for binning 2 (`ShadowModel.binned`), marked ``derived``, so a
  solve of a 2x2 frame finds a model without a detection of its own. A model detected on a binning 2 frame replaces the
  derived one, and a later binning 1 detection never replaces a native binning 2 entry.

The band is where the mirror puts it, and the mirror is where the stage puts it, so a model detected at a stage position
describes the band at that position, whatever asks for it and whenever: an acquisition that begins after a restart, the
guider started on its own (no acquisition, so no `Acquisition.shadow` to inherit), a solve in another process. Detection
costs 7 to 8 s on a full frame; looking a model up costs a file read.

* **Keyed by the stage position and the frame's shape.** Whether a stored position is the stage's position is the stage's own
  judgement: callers pass `Stage.close_enough` (the stage's current position against a target, within its configured
  tolerance), and this module compares nothing itself. The phase is recorded, not matched: it names what the solve is for,
  not where the mirror is.
* **Only a band that was found is kept.** "No band" is a result worth caching in one acquisition's memory, but on disk a
  single frame taken through cloud would stand for the position until someone noticed.
* **A model is a measurement and can go stale** (the stage's repeatability, a service on the mirror). Every entry says when
  and on what frame it was detected; a caller can refuse an old one (``max_age_days``), `get_shadow(refresh=True)` detects
  again and replaces it, and `ShadowStore.remove` drops one.
* **Nothing here raises**: a store that cannot be read or written is an empty one, and a lookup that fails is a detection.

`get_shadow` is the one call for a caller that has a frame, a stage position and no acquisition.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

from common.mast_logging import get_logger
from imaging.mirror_shadow import ShadowModel, detect_mirror_shadow

logger = get_logger(__name__)

#: Whether a stored stage position is where the stage is now: `Stage.close_enough`, or `within`.
IsClose = Callable[[int], bool]


def within(position: int | None, tolerance: int) -> IsClose:
    """An `IsClose` for a stage that has no `close_enough` of its own: positions within ``tolerance`` of ``position``."""
    return lambda stored: position is not None and abs(int(stored) - int(position)) <= int(tolerance)


CALIBRATION_FOLDER = "Calibration"
STORE_FILE_NAME = "shadow_models.json"
VERSION = 1


@dataclass
class StoredShadow:
    """One model, and what it was detected from."""

    model: ShadowModel
    stage_position: int
    detected_at: str  # ISO UTC
    seconds: float  # what the detection cost
    image_path: str  # the frame it was detected on
    phase: str | None = None
    #: the binning of the frame the model describes (1: the full sensor)
    binning: int = 1
    #: made from the binning 1 model by `ShadowModel.binned`, not detected on a frame of this binning
    derived: bool = False

    def age_days(self, now: datetime.datetime | None = None) -> float:
        now = now or datetime.datetime.now(datetime.UTC)
        try:
            detected = datetime.datetime.fromisoformat(self.detected_at)
        except ValueError:
            return float("inf")
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=datetime.UTC)
        return (now - detected).total_seconds() / 86400.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_position": int(self.stage_position),
            "detected_at": self.detected_at,
            "seconds": round(float(self.seconds), 2),
            "image_path": self.image_path,
            "phase": self.phase,
            "binning": int(self.binning),
            "derived": bool(self.derived),
            "model": self.model.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoredShadow:
        return cls(
            model=ShadowModel.from_dict(d["model"]),
            stage_position=int(d["stage_position"]),
            detected_at=str(d["detected_at"]),
            seconds=float(d.get("seconds", 0.0)),
            image_path=str(d.get("image_path", "")),
            phase=d.get("phase"),
            binning=int(d.get("binning", 1)),
            derived=bool(d.get("derived", False)),
        )


#: The binnings a model detected at binning 1 is also kept for.
DERIVED_BINNINGS = (2,)


def frame_binning(frame) -> int:
    """The binning of a frame: ``XBINNING`` in the FITS header of the file at a path, else 1 (an array says nothing)."""
    if isinstance(frame, np.ndarray):
        return 1
    try:
        return max(1, int(fits.getheader(Path(frame), 0).get("XBINNING", 1)))
    except (OSError, ValueError, TypeError):
        return 1


def frame_shape(frame) -> tuple[int, int] | None:
    """``(ny, nx)`` of an array, or of the FITS file at a path (read from its header). None if unknown."""
    if isinstance(frame, np.ndarray):
        return (int(frame.shape[0]), int(frame.shape[1]))
    try:
        header = fits.getheader(Path(frame), 0)
        return (int(header["NAXIS2"]), int(header["NAXIS1"]))
    except (OSError, KeyError, ValueError, TypeError):
        return None


class ShadowStore:
    def __init__(self, unit_folder: Path | str):
        self.path = Path(unit_folder) / CALIBRATION_FOLDER / STORE_FILE_NAME

    @classmethod
    def for_machine(cls) -> ShadowStore | None:
        """This machine's store (its local filing area), or None where there is no such folder to name."""
        try:
            from common.filer import Filer  # noqa: PLC0415 -- builds the Filer; not at import

            return cls(Filer().local.root)
        except Exception as ex:  # noqa: BLE001 -- no store: the shadow is detected, as before
            logger.warning(f"no shadow store ({ex!r})")
            return None

    # ------------------------------------------------------------------------------------------------ reading

    def entries(self) -> list[StoredShadow]:
        """Every entry in the file; none if it is missing, damaged or of another version."""
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as ex:
            logger.warning(f"shadow store '{self.path}' is unreadable ({ex!r}): treated as empty")
            return []
        if not isinstance(document, dict) or document.get("version") != VERSION:
            version = document.get("version") if isinstance(document, dict) else "?"
            logger.warning(f"shadow store '{self.path}' is of version {version!r}, expected {VERSION}")
            return []
        found: list[StoredShadow] = []
        for raw in document.get("entries", []):
            try:
                found.append(StoredShadow.from_dict(raw))
            except (KeyError, ValueError, TypeError) as ex:
                logger.warning(f"shadow store '{self.path}': an entry was skipped ({ex!r})")
        return found

    def find(
        self,
        stage_position: int | None,
        is_close: IsClose,
        image_shape: tuple[int, int] | None,
        max_age_days: float | None = None,
    ) -> StoredShadow | None:
        """The entry nearest ``stage_position`` for a frame of ``image_shape`` whose position ``is_close`` accepts (the
        stage's `close_enough`), or None.

        None when the stage cannot say where it is or the frame's shape is unknown: there is nothing to key on."""
        if stage_position is None or image_shape is None:
            return None
        best: StoredShadow | None = None
        for entry in self.entries():
            if tuple(entry.model.image_shape) != tuple(image_shape):
                continue
            if max_age_days is not None and entry.age_days() > max_age_days:
                continue
            if not is_close(entry.stage_position):
                continue
            distance = abs(int(entry.stage_position) - int(stage_position))
            if best is None or distance < abs(int(best.stage_position) - int(stage_position)):
                best = entry
        return best

    # ------------------------------------------------------------------------------------------------ writing

    def save(self, entry: StoredShadow, is_close: IsClose) -> bool:
        """Keep ``entry``, replacing any entry for the same frame shape whose position ``is_close`` accepts as its own.

        An entry of binning 1 is kept for the `DERIVED_BINNINGS` as well, as a derived entry; one of those replaces a derived
        entry and never a native one. The file is replaced whole, so a reader never sees a half-written one. False (logged)
        if it could not be written."""

        def same(e: StoredShadow, other: StoredShadow) -> bool:
            return tuple(e.model.image_shape) == tuple(other.model.image_shape) and is_close(e.stage_position)

        entries = [e for e in self.entries() if not same(e, entry)]
        added = [entry]
        if entry.binning == 1 and entry.model.present:
            ny, nx = entry.model.image_shape
            for factor in DERIVED_BINNINGS:
                if ny % factor or nx % factor:
                    continue  # the blocks would not tile the frame
                derived = dataclasses.replace(entry, model=entry.model.binned(factor), binning=factor, derived=True)
                if any(same(e, derived) and not e.derived for e in entries):
                    continue  # a model detected on a frame of this binning is the better one
                entries = [e for e in entries if not same(e, derived)]
                added.append(derived)
        return self._write([*entries, *added])

    def remove(self, is_close: IsClose, image_shape: tuple[int, int] | None = None) -> int:
        """Drop the entries whose position ``is_close`` accepts (for ``image_shape``, if given). How many."""
        entries = self.entries()
        kept = [
            e
            for e in entries
            if not (is_close(e.stage_position) and (image_shape is None or tuple(e.model.image_shape) == tuple(image_shape)))
        ]
        if len(kept) != len(entries):
            self._write(kept)
        return len(entries) - len(kept)

    def _write(self, entries: list[StoredShadow]) -> bool:
        document = {"version": VERSION, "entries": [e.to_dict() for e in entries]}
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
            return True
        except OSError as ex:
            logger.warning(f"could not write the shadow store '{self.path}' ({ex!r})")
            return False


# ------------------------------------------------------------------------------------------------ the one call


@dataclass
class ShadowResult:
    model: ShadowModel | None
    #: "stored" (read from the store), "detected" (just detected, and kept if a band was found) or "failed"
    source: str
    entry: StoredShadow | None = None


def get_shadow(
    frame,
    stage_position: int | None,
    is_close: IsClose,
    *,
    store: ShadowStore | None,
    phase: str | None = None,
    detector: Callable[..., ShadowModel] = detect_mirror_shadow,
    refresh: bool = False,
    max_age_days: float | None = None,
) -> ShadowResult:
    """The shadow model for a frame taken with the stage at ``stage_position``: from the store if there is one for that
    position and shape (``is_close`` is the stage's `close_enough`), else detected on ``frame`` (an array or a FITS path),
    and kept when a band was found.

    ``refresh`` detects whatever is stored and replaces it. Without a stage position nothing is looked up or kept: there is
    nothing to key on. Never raises: a detection that fails is ``source == "failed"`` and no model."""
    shape = frame_shape(frame)
    if store is not None and not refresh:
        stored = store.find(stage_position, is_close, shape, max_age_days)
        if stored is not None:
            logger.info(
                f"shadow model for stage position {stage_position}: stored, detected {stored.detected_at} "
                f"at position {stored.stage_position} ({stored.age_days():.1f} days old)"
            )
            return ShadowResult(stored.model, "stored", stored)
    started = time.monotonic()
    try:
        model = detector(frame)
    except Exception as ex:  # noqa: BLE001 -- a shadow we cannot find must not cost the solve
        logger.warning(f"shadow detection failed ({ex!r})")
        return ShadowResult(None, "failed")
    seconds = time.monotonic() - started
    entry = None
    if store is not None and stage_position is not None and model.present and shape is not None:
        entry = StoredShadow(
            model=model,
            stage_position=int(stage_position),
            detected_at=datetime.datetime.now(datetime.UTC).isoformat(),
            seconds=seconds,
            image_path=str(frame) if not isinstance(frame, np.ndarray) else "",
            phase=phase,
            binning=frame_binning(frame),
        )
        if store.save(entry, is_close):
            logger.info(
                f"shadow model for stage position {stage_position} detected in {seconds:.1f} s and kept in '{store.path}'"
            )
        else:
            entry = None
    return ShadowResult(model, "detected", entry)
