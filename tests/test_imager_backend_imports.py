"""Every imager backend must import.

`Imager.__init__` imports its backend **lazily**, inside the branch for the configured
`imager_type`. An ImportError there aborts `__init__` before `self._backend` is assigned,
and every later attribute access then reports ``'Imager' object has no attribute
'_backend'`` -- with the real cause several frames away. Nothing else imports these
modules at module scope, so neither app startup nor the rest of this suite notices when
one goes stale; #71 sat broken in both backends for exactly that reason.

This is the cheapest possible guard: import each backend and let the failure name itself.
"""

from __future__ import annotations

import importlib
import platform
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("win32com", reason="the unit's imager backends are Windows-only")

# Every backend a configured `imager_type` can select. `Imager` itself is the delegating
# wrapper, imported here too because a break in it hides every backend behind it.
BACKEND_MODULES = ["imagers", "imagers.ascom", "zwo", "phd2.phd2"]


@pytest.fixture(scope="module", autouse=True)
def _hermetic_filer():
    """Keep the module-level ``Filer`` in ``common.utils`` off the unit's storage roots.

    conftest shims this for Darwin, where the import fails outright; on Windows it
    succeeds but would reach for real paths. Applied before any import below.
    """
    if platform.system() != "Windows":
        yield
        return

    import common.filer as filer_module

    location = filer_module.Location(None, str(Path(tempfile.mkdtemp(prefix="mast-import-tests-"))))
    original_init = filer_module.Filer.__init__

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
    yield
    filer_module.Filer.__init__ = original_init


@pytest.mark.parametrize("module_name", BACKEND_MODULES)
def test_imager_backend_imports(module_name):
    """A stale name in one of these is invisible until a unit starts up (#71)."""
    try:
        assert importlib.import_module(module_name) is not None
    except ImportError as exc:
        pytest.fail(
            f"{module_name} does not import: {exc}\n"
            "Imager.__init__ imports backends lazily, so this surfaces on a unit as "
            "\"'Imager' object has no attribute '_backend'\" -- see #71."
        )
