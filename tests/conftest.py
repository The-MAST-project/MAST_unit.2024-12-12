"""Test bootstrap for the MAST_unit suite.

Puts ``src/`` on ``sys.path`` so the tests import exactly what the unit
service imports. ``common`` is NOT under this repo: it is a sibling clone
(``<top>/common/``) reached through the ``mast.pth`` the provisioning writes
into the venv, so nothing here needs to place it -- but the tests will fail to
import it in a venv without that ``.pth``.

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

Also blocks external process launches, so a test cannot drive real hardware --
see ``_block_external_processes`` below.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# Programs a test may legitimately start. Keep this list short and justified: it is the
# one hole in the guard below.
#
# fontconfig -- matplotlib's font manager shells out to `fc-list` while importing. That
# happens on Linux only, so this repo (Windows-only CI) will not hit it, but the entry is
# kept in step with MAST_common so the two guards cannot drift, and so the suite still
# runs on a developer's Linux box.
ALLOWED = {"fc-list", "fc-match"}


class ProcessLaunchError(RuntimeError):
    """Raised when a test tries to start an external process."""


def _block_external_processes() -> None:
    """Fail loudly if anything under test starts an external process.

    Installed at conftest IMPORT time, not from a fixture. Fixtures -- even autouse
    session ones -- first run when the first test runs, which is after collection, and
    collection imports every test module. A test module that imports an entry point at
    top level would therefore spawn before any fixture could stop it. conftest is
    imported before collection begins, so patching here covers that window too.

    Nothing is started and then killed: the replacements raise instead of spawning, so
    no process is ever created.

    The units run the test suite on the same machines that run the telescope, so a
    test that launches a process does not merely pollute a sandbox -- it starts PWI4,
    the shutter, or the plate solver against real hardware. That is not hypothetical:
    ``src/app.py`` calls ``ensure_process_is_running`` at MODULE level (its
    ``if __name__ == "__main__"`` is far below), so simply importing it is enough, and
    doing so once during this repo's ruff clean-up did exactly that.

    Nothing in this suite has any business spawning a process, so the low-level entry
    points are blocked wholesale rather than any single funnel being patched: a direct
    ``subprocess.Popen`` would slip past a patch of ``ensure_process_is_running``. If a
    test ever needs a real subprocess, allow it explicitly here rather than removing
    the guard.

    On a CI runner the guarded programs are not installed, so a spawn would fail
    there anyway -- just slowly and with a confusing error. The case this exists for is
    the unit machines, where the suite is run and the programs are real.

    This is a tripwire, not the fix. The fix is for app.py to start nothing on import.
    """
    denied = {
        subprocess: ("Popen", "run", "call", "check_call", "check_output"),
        os: ("system", "popen", "startfile", "execv", "execvp", "spawnl", "spawnv"),
    }
    # common.process spawns through subprocess.Popen and uses psutil only to FIND
    # processes -- but psutil.Popen exists, so close that door before someone reaches
    # for it.
    try:
        import psutil

        denied[psutil] = ("Popen",)
    except ImportError:
        pass

    def executable_of(target) -> str:
        """Best-effort program name, whether the caller passed a list or a string."""
        if isinstance(target, (list, tuple)) and target:
            target = target[0]
        return os.path.basename(str(target)).lower()

    def deny(name, original):
        def _deny(*args, **kwargs):
            target = args[0] if args else kwargs.get("args") or kwargs.get("cmd") or "?"
            if executable_of(target) in ALLOWED:
                return original(*args, **kwargs)
            raise ProcessLaunchError(
                f"the test suite tried to start a process via {name}: {target!r}. "
                "On a unit machine this would drive real hardware. If a test genuinely "
                "needs this, allow it explicitly in tests/conftest.py."
            )

        return _deny

    for module, names in denied.items():
        for name in names:
            original = getattr(module, name, None)
            if original is None:  # os.startfile is Windows-only, etc.
                continue
            setattr(module, name, deny(f"{module.__name__}.{name}", original))


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


_block_external_processes()
