"""Test bootstrap for the MAST_unit suite.

Puts ``src/`` on ``sys.path`` so the tests import exactly what the unit
service imports (``common`` resolves to the ``src/common`` submodule).

On Darwin only, ``common.filer.Filer.__init__`` is shimmed to a temp-dir
layout: ``Filer`` supports Windows/Linux only and raises at import time on
macOS (``common.utils`` builds a module-level ``Filer``), and its Linux paths
(``/Storage/...``) are unwritable there anyway. The shim is a no-op on the
platforms the code deploys to; retire it if Darwin support ever lands.
"""

from __future__ import annotations

import platform
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _shim_filer_for_darwin() -> None:
    if platform.system() != "Darwin":
        return
    import common.filer as filer_module

    tmp_root = tempfile.mkdtemp(prefix="mast-unit-tests-")
    location = filer_module.Location(None, tmp_root)

    def _darwin_init(self, logger=None):
        self.local = location
        self.shared = location
        self.ram = location
        self.tops = {
            filer_module.FilerTop.Local: self.local,
            filer_module.FilerTop.Shared: self.shared,
            filer_module.FilerTop.Ram: self.ram,
        }
        self.logger = logger

    filer_module.Filer.__init__ = _darwin_init


_shim_filer_for_darwin()
