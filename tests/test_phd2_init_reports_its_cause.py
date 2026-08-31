"""A failed PHD2 construction reports what actually failed, once per site (#84).

Observed on mast01, 2026-08-04: the ZWO camera was unplugged, PHD2 answered and said
`equipment failed to connect: camera` -- and the unit reported `no connection to PHD2
server`, which was false; PHD2 had already returned its version on that very connection.
Two `phd2.exe` were alive, the second showing PHD2's "an instance is already running"
dialog.

One mechanism, two effects. `__init__`'s handler ran `self.connected = False`, whose setter
disconnects and nulls `self.conn`; the next statement, `self.cooler_on = True`, sat outside
the guard and its setter is a bare `call()`, which raises on a null conn. So the reported
cause was manufactured three steps downstream of the real one. And `_initialized` was
assigned on the last line, so an escape left `__new__`'s cache holding a half-built
instance: the second construction site failed the guard, re-ran the body, and launched PHD2
again.

Dynamic where the seam allows it and static where a real PHD2 would be needed: the guard,
the `cooler_on` setter and `__del__` are all reachable off-hardware, but the try/finally
around the connect chain is not -- the surrounding body powers outlets, reads config,
locates phd2.exe and sleeps 3 seconds. Those two lines are pinned by source inspection.
"""

from __future__ import annotations

import inspect

import pytest

from phd2.phd2 import PHD2Connector


@pytest.fixture(autouse=True)
def _no_singleton_leak():
    """The cache and both flags are class attributes; a test that sets them leaks."""
    saved = (PHD2Connector._instance, PHD2Connector._initialized, PHD2Connector._init_error)
    PHD2Connector._instance = None
    PHD2Connector._initialized = False
    PHD2Connector._init_error = None
    yield
    PHD2Connector._instance, PHD2Connector._initialized, PHD2Connector._init_error = saved


def _half_built() -> PHD2Connector:
    """An instance as `__new__` leaves it: cached, with `__init__` not yet run."""
    return object.__new__(PHD2Connector)


class _BoomError(Exception):
    pass


# --- the second construction site hears about the first one's failure ------------------


def test_a_remembered_failure_is_re_raised_for_the_next_site():
    inst = _half_built()
    inst._initialized = True
    inst._init_error = _BoomError("equipment failed to connect: camera")

    with pytest.raises(_BoomError, match="equipment failed to connect: camera"):
        PHD2Connector.__init__(inst)


def test_the_re_raised_error_is_the_original_object():
    """Not a re-wrap: the imager and the guider must report the same cause."""
    inst = _half_built()
    original = _BoomError("the real one")
    inst._initialized = True
    inst._init_error = original

    with pytest.raises(_BoomError) as caught:
        PHD2Connector.__init__(inst)

    assert caught.value is original


def test_a_successful_singleton_still_short_circuits():
    inst = _half_built()
    inst._initialized = True
    inst._init_error = None

    assert PHD2Connector.__init__(inst) is None


def test_the_guard_runs_before_anything_is_launched():
    """A remembered failure must refuse without reaching WatchedProcess.start()."""
    inst = _half_built()
    inst._initialized = True
    inst._init_error = _BoomError("x")

    with pytest.raises(_BoomError):
        PHD2Connector.__init__(inst)

    assert not hasattr(inst, "watched_process")


# --- the cooler assignment cannot manufacture an error --------------------------------


def test_the_cooler_setter_records_instead_of_raising():
    inst = _half_built()
    inst.errors = []

    def _raise(*_args, **_kwargs):
        raise RuntimeError("no connection to PHD2 server")

    inst.call = _raise
    PHD2Connector.cooler_on.fset(inst, True)

    assert len(inst.errors) == 1
    assert "no connection to PHD2 server" in inst.errors[0]


def test_the_cooler_setter_passes_the_value_through_when_it_works():
    inst = _half_built()
    inst.errors = []
    seen = []
    inst.call = lambda method, *args: seen.append((method, args))

    PHD2Connector.cooler_on.fset(inst, True)

    assert seen == [("set_cooler_state", (True,))]
    assert inst.errors == []


# --- teardown of a half-built instance ------------------------------------------------


def test_del_on_a_half_built_instance_is_silent():
    """This raises on main today, visibly, as a PytestUnraisableExceptionWarning."""
    inst = _half_built()

    PHD2Connector.__del__(inst)


def test_del_still_terminates_a_process_it_has():
    terminated = []
    inst = _half_built()
    inst.watched_process = type("_P", (), {"terminate": lambda self: terminated.append(True)})()

    PHD2Connector.__del__(inst)

    assert terminated == [True]
    assert inst.watched_process is None


# --- the two lines a real PHD2 would be needed to exercise ----------------------------


def _init_source() -> str:
    return inspect.getsource(PHD2Connector.__init__)


def test_the_flag_is_set_in_a_finally():
    source = _init_source()
    finally_at = source.index("finally:")

    assert "self._initialized = True" in source[finally_at:], (
        "_initialized must be set on the failure path too, or the second construction "
        "site re-runs __init__ and launches a second phd2.exe"
    )


def test_the_cooler_assignment_is_inside_the_guard():
    source = _init_source()

    assert source.index("self.cooler_on = True") < source.index("except Exception"), (
        "outside the guard, its setter's error replaces the real failure as the reported cause"
    )


def test_the_handler_does_not_assign_the_connected_property():
    """`self.connected = False` is two teardown calls wearing a status assignment."""
    code = [line.split("#", 1)[0] for line in _init_source().splitlines()]

    assert not any("self.connected = False" in line for line in code)
    assert any("self._connected = False" in line for line in code)
