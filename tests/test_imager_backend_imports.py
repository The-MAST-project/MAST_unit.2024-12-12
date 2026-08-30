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

import pytest

# No platform guard: `conftest` stubs the absent hardware modules, so this runs on a dev
# machine as well as on a unit (#52). Windows keeps the real modules -- only absent ones
# are stubbed -- so nothing here can mask genuine Windows behaviour.

# Every backend a configured `imager_type` can select. `Imager` itself is the delegating
# wrapper, imported here too because a break in it hides every backend behind it.
BACKEND_MODULES = ["imagers", "imagers.ascom", "zwo", "phd2.phd2"]

# The module-level ``Filer`` that ``common.utils`` builds on import is kept off the unit's
# real storage roots by ``conftest``, which shims it on every platform (#76). This module
# carried its own copy of that shim while it lived on a branch that could not depend on
# the hoist; conftest owns it now.


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
