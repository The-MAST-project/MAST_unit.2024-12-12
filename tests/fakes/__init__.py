"""Stand-ins that let the unit's component modules import and run off a unit (#52).

Two separate jobs, deliberately kept apart:

- `install_hardware_stubs()` makes the *imports* succeed. `mount`, `focuser`, `stage` and the
  imager backends bind `win32com`, `pyximc` or `pyzwoasi` at module scope, so on any machine
  without them the module cannot be imported at all -- which is why six test modules skip on a
  dev machine and the behavioural half of the suite has only ever run on Windows.
- The `pwi4` and `ximc` fakes are *devices*: small state machines a test drives explicitly.

A stub is installed **only when the real module is absent**, so Windows CI keeps importing the
real thing and nothing here can mask a genuine Windows behaviour.
"""

from __future__ import annotations

import importlib.util
import sys
import types

#: Module-scope imports that do not exist off a unit. Each maps to the attributes the unit's
#: modules reach for at import time -- nothing here models behaviour; the device fakes do that.
_HARDWARE_MODULES = (
    "win32com",
    "win32com.client",
    "pythoncom",
    "pywintypes",
    "win32api",
    "win32con",
    "pyzwoasi",
    "pyximc",
)


def _is_available(name: str) -> bool:
    """Whether the real module can actually be imported here.

    An import attempt, not `find_spec`: `pyzwoasi` resolves on any platform because it is a
    pip package, and then fails to load its native library off Windows. Asking whether the
    file exists answers the wrong question -- what matters is whether importing it works.
    """
    try:
        importlib.import_module(name)
    except Exception:  # noqa: BLE001 -- any failure to import means "use a stub"
        return False
    return True


class _Stub(types.ModuleType):
    """Answers any attribute with another stub, so an import-time lookup cannot fail.

    Deliberately permissive: the point is to get past `import` and the module-scope name
    binding, not to emulate an API. Anything a test actually exercises is given a real fake.
    """

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        child = _Stub(f"{self.__name__}.{name}")
        setattr(self, name, child)
        return child

    def __call__(self, *args, **kwargs):
        return _Stub(f"{self.__name__}()")


def install_hardware_stubs() -> list[str]:
    """Install a stub for each absent hardware module. Returns the names stubbed."""
    stubbed = []
    for name in _HARDWARE_MODULES:
        if name in sys.modules or _is_available(name):
            continue
        module = _Stub(name)
        sys.modules[name] = module
        if "." in name:
            parent, _, attribute = name.rpartition(".")
            setattr(sys.modules[parent], attribute, module)
        stubbed.append(name)
    return stubbed
