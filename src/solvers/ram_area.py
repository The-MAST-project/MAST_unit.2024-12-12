"""Where the solvers find the astrometry indexes and put their scratch.

plans/filing-areas-design.md 7.1, item 9. Two different places, deliberately:

- The **indexes** live on the RAM disk itself (``D:/mast-indexes``), loaded with its image
  at boot. They are never on the RAM fallback: while the disk is gone, so are they, and the
  disk's revival from its image brings them back.
- The **scratch** follows the RAM filing area: the disk, or its fallback while the disk is
  gone, so a solve does not fail because the folder it was handed at start-up vanished.
"""

from __future__ import annotations

import errno
from pathlib import Path, PureWindowsPath

from common.filer import Filer, FilingArea

INDEX_FOLDER = "mast-indexes"


def index_dir() -> Path:
    """``<RAM disk's drive>/mast-indexes``: beside ``D:/MAST/``, not under it.

    From the RAM disk's own root, never the current one -- the indexes come with the disk.
    Off Windows, where a root has no drive (tests), the folder beside the RAM root.
    """
    ram = Filer().ram
    assert ram is not None, "no RAM area on this machine"
    drive = PureWindowsPath(ram.root).drive
    base = f"{drive}/" if drive else str(Path(ram.root).parent)
    return Path(base, INDEX_FOLDER)


def scratch_dir(name: str) -> Path:
    """``<RAM area's current root>/tmp/<name>``, created. Raises OSError (ENOSPC) when there
    is nowhere to write: the RAM disk gone and the local disk at its ceiling."""
    root = Filer().current_root(FilingArea.RAM)
    if root is None:
        raise OSError(errno.ENOSPC, "no RAM filing area for solver scratch: the RAM disk is gone and the local disk is full")
    path = Path(root, "tmp", name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def missing_indexes(index: Path) -> str | None:
    """Why solving cannot run when ``index`` is not there, or None when it is.

    Said plainly, since the solver's own failure for a missing index folder is generic.
    """
    if index.is_dir():
        return None
    why = f"the astrometry indexes are missing: '{index.as_posix()}' is not on the RAM disk"
    if Filer().is_demoted(FilingArea.RAM):
        why += " -- the RAM disk is on its fallback; solving resumes once it is back, indexes and all"
    return why
