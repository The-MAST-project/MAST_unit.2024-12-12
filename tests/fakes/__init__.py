"""Stand-ins that let the unit's component modules import off a unit (#52).

`install_hardware_stubs()` makes the *imports* succeed, and that is the whole of it. `mount`,
`focuser`, `stage` and the imager backends bind `win32com`, `pyximc` or `pyzwoasi` at module
scope, so without those modules the unit's own modules cannot be imported at all and every
test touching them skips. A stub is installed **only when the real module is absent**, so
Windows CI keeps importing the real thing and nothing here can mask a genuine Windows
behaviour.

**Nothing in this package models a device.** There are no `pwi4` or `ximc` fakes. A test that
needs device behaviour builds it inline, which is why `MagicMock` and `monkeypatch` are spread
through the behavioural modules rather than collected here.

#52's Phase 1 designed a shared device layer -- fake ASCOM `Dispatch` objects per prog-id, a
scriptable `pwi4_client` with slew / move / exposure state machines -- and it was never built.
Read that issue before building one. Most of what such a layer would stand in for is a
constructor reaching for hardware, so making construction inert is the cheaper fix and removes
the need rather than meeting it (#91, #111, #197). Where a real dependency is easier to stand
up than to imitate, prefer it: `pwi4_client` speaks HTTP, so a real `http.server` on an
ephemeral port is a better test double than a fake client.
"""

from __future__ import annotations

import importlib.util
import sys
import types

#: Module-scope imports that do not exist off a unit. Stubbing one gets the unit's modules
#: past their import-time name binding; nothing here models behaviour.
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
    binding, not to emulate an API. A test that exercises the thing itself supplies its own
    stand-in at the call site.
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
