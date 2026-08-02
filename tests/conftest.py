"""Test bootstrap for the MAST_unit suite.

Puts ``src/`` on ``sys.path`` so the tests import exactly what the unit
service imports (``common`` resolves to the ``src/common`` submodule).

``common.filer.Filer.__init__`` is shimmed to a temp-dir layout on **every**
platform, because ``common.utils`` builds a module-level ``Filer`` at import,
so every component module drags one in:

- On **macOS** the shim is what makes the import possible at all: ``Filer``
  supports Windows/Linux only and raises at import time, and its Linux paths
  (``/Storage/...``) are unwritable there anyway.
- On **Windows** the import succeeds, but an unshimmed ``Filer`` reaches for
  the unit's real storage roots, which a test suite has no business touching.

Retire the macOS rationale if Darwin support ever lands; keep the temp-dir
redirection regardless.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _shim_filer_for_tests() -> None:
    import common.filer as filer_module

    tmp_root = tempfile.mkdtemp(prefix="mast-unit-tests-")
    location = filer_module.Location(None, tmp_root)

    def _tmp_init(self, logger=None):
        self.local = location
        self.shared = location
        self.ram = location
        self.tops = {
            filer_module.FilerTop.Local: self.local,
            filer_module.FilerTop.Shared: self.shared,
            filer_module.FilerTop.Ram: self.ram,
        }
        self.logger = logger

    filer_module.Filer.__init__ = _tmp_init


_shim_filer_for_tests()
