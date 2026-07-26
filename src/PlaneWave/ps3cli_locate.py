"""
Locate the ps3cli (PlateSolve3) executable and its star catalog.

Single source of truth for ps3cli discovery, shared by app.py (which starts
``ps3cli --server`` at unit startup) and the provisioning autofocus-solve
validator (which starts a throwaway server). Both must resolve the exe and
catalog identically, so the logic lives here rather than being duplicated.
"""

import os
from pathlib import Path


def locate_ps3cli_dir() -> str | None:
    """
    Return the directory containing ps3cli.exe, or None.

    The special --server build ships inside a dated folder (e.g. ps3cli-2024-09-10)
    whose name changes per build, so search recursively under known roots rather
    than hardcoding the subdirectory. PS3CLI_DIR (set machine-wide by the planewave
    provider) wins if present. Picks the largest ps3cli.exe: the special --server
    build (~4 MB) beats any stale older on-demand build (~10 KB) left beside it.
    """
    roots = [
        Path(os.environ["PS3CLI_DIR"]) if os.environ.get("PS3CLI_DIR") else None,
        Path.home() / "Documents" / "PlaneWave" / "ps3cli",
        Path(r"C:\Program Files (x86)\PlaneWave Instruments\ps3cli"),
    ]
    for root in roots:
        if not root or not root.is_dir():
            continue
        exe = max(
            (p for p in root.rglob("ps3cli.exe") if p.is_file()),
            key=lambda p: p.stat().st_size,
            default=None,
        )
        if exe:
            return str(exe.parent)
    return None


def locate_ps3cli_catalog() -> str | None:
    """
    Return the PlateSolve3 star-catalog directory (one containing UC4 and Orca
    subdirectories), or None. This is separate from the astrometry.net indexes
    and NOT interchangeable with them. Without it, ``ps3cli --server`` validates
    the catalog at startup and exits. PS3CLI_CATALOG wins if present.
    """

    def _is_catalog(p: Path) -> bool:
        return (p / "UC4").is_dir() and (p / "Orca").is_dir()

    roots = [
        Path(os.environ["PS3CLI_CATALOG"]) if os.environ.get("PS3CLI_CATALOG") else None,
        Path.home() / "Documents" / "Kepler",
        Path.home() / "Downloads" / "PlaneWave" / "Platesolve3.80" / "Kepler",
    ]
    for root in roots:
        if not root or not root.is_dir():
            continue
        if _is_catalog(root):
            return str(root)
        for sub in root.iterdir():
            if sub.is_dir() and _is_catalog(sub):
                return str(sub)
    return None
