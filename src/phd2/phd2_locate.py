"""
Locate the PHD2 guiding application executable (phd2.exe).

Single source of truth for PHD2 discovery, mirroring PlaneWave/ps3cli_locate.py.
The PHD2 installer's on-disk layout has drifted across versions and architectures
(``Program Files`` vs ``Program Files (x86)``, ``PHDGuiding2`` vs ``PHD2``), and the
provisioning provider installs it by running the vendor installer and discovering
the exe afterwards rather than forcing a fixed path. So resolve the executable at
runtime rather than hardcoding one path. PHD2_EXE wins if set.
"""

import os
from pathlib import Path


def locate_phd2_exe() -> str | None:
    """
    Return the full path to phd2.exe, or None if not found.

    Resolution order:
      1. PHD2_EXE environment variable (explicit override), if it points at a file.
      2. Known install locations, most-likely first.
      3. Recursive search under the Program Files roots (last resort).
    """
    override = os.environ.get("PHD2_EXE")
    if override and Path(override).is_file():
        return str(Path(override))

    candidates = [
        Path(r"C:\Program Files (x86)\PHDGuiding2\phd2.exe"),
        Path(r"C:\Program Files\PHDGuiding2\phd2.exe"),
        Path(r"C:\Program Files (x86)\PHD2\phd2.exe"),
        Path(r"C:\Program Files\PHD2\phd2.exe"),
    ]
    for exe in candidates:
        if exe.is_file():
            return str(exe)

    for root in (Path(r"C:\Program Files (x86)"), Path(r"C:\Program Files")):
        if not root.is_dir():
            continue
        exe = next((p for p in root.rglob("phd2.exe") if p.is_file()), None)
        if exe:
            return str(exe)

    return None
