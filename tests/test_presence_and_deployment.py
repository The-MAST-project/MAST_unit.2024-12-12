"""The mount and focuser report presence separately from deployment (MAST_unit#175, #144).

`Mount.connected` used to require both servo axes enabled, so every reader of it -- the periodic
timer, `status()`, the settle wait -- meant *reachable and deployed*. A mount that was connected
with its axes down therefore stopped being polled, published nothing, and answered every settle
wait with "settled".

These tests dictate PWI4's answers rather than talking to a mount: the point under test is which
terms each predicate reads, which is where the conflation lived.
"""

from __future__ import annotations

import pytest


class FakeAxis:
    def __init__(self, is_enabled: bool):
        self.is_enabled = is_enabled


class FakeMountSection:
    def __init__(self, *, is_connected: bool, axes_enabled: bool):
        self.is_connected = is_connected
        self.axis0 = FakeAxis(axes_enabled)
        self.axis1 = FakeAxis(axes_enabled)


class FakeFocuserSection:
    def __init__(self, *, exists: bool, is_connected: bool, is_enabled: bool, position: float):
        self.exists = exists
        self.is_connected = is_connected
        self.is_enabled = is_enabled
        self.position = position


class FakeStatus:
    def __init__(self, mount=None, focuser=None):
        self.mount = mount
        self.focuser = focuser


class FakePwi4:
    """Records what was commanded, so a refusal can be told from a silent no-op."""

    def __init__(self, status: FakeStatus):
        self._status = status
        self.calls: list[str] = []

    def status(self) -> FakeStatus:
        return self._status

    def request(self, path: str, **_kwargs):
        self.calls.append(f"request:{path}")

    def __getattr__(self, name: str):
        def recorder(*args, **_kwargs):
            self.calls.append(name if not args else f"{name}{args}")

        return recorder


class Succeeded:
    succeeded = True
    failed = False
    value = True
    failure = None


class Failed:
    succeeded = False
    failed = True
    value = None
    failure = "no handle"


def _mount_module():
    try:
        import mount
    except (ImportError, NameError) as ex:  # win32com: Windows-only, MAST_unit#118
        pytest.skip(f"mount is not importable here: {type(ex).__name__}: {ex}")
    return mount


def _focuser_module():
    try:
        import focuser
    except (ImportError, NameError) as ex:
        pytest.skip(f"focuser is not importable here: {type(ex).__name__}: {ex}")
    return focuser


def _mount(monkeypatch, *, on=True, is_connected=True, axes_enabled=False, ascom=Succeeded):
    module = _mount_module()
    monkeypatch.setattr(module, "ascom_run", lambda *_args, **_kwargs: ascom())

    class FakeMount(module.Mount):
        def is_on(self) -> bool:
            return on

    mount = object.__new__(FakeMount)
    mount._ascom = object()
    mount.pw = FakePwi4(FakeStatus(mount=FakeMountSection(is_connected=is_connected, axes_enabled=axes_enabled)))
    mount.errors = []
    return mount


def test_connected_no_longer_requires_the_axes():
    """The property every reader treats as "comms are up" now means exactly that."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        mount = _mount(monkeypatch, axes_enabled=False)

        assert mount.connected is True
        assert mount.reachable is True
        assert mount.deployed is False


def test_a_connected_mount_with_energized_axes_is_deployed():
    with pytest.MonkeyPatch.context() as monkeypatch:
        mount = _mount(monkeypatch, axes_enabled=True)

        assert mount.deployed is True
        assert mount.operational is True
        assert mount.why_not_operational == []


def test_the_reasons_name_the_axes_rather_than_the_connection():
    with pytest.MonkeyPatch.context() as monkeypatch:
        mount = _mount(monkeypatch, axes_enabled=False)

        assert mount.why_not_reachable == []
        assert mount.why_not_deployed is not None
        assert "axis0 not enabled" in mount.why_not_deployed[0]
        assert mount.why_not_operational == mount.why_not_deployed


def test_an_unreachable_mount_says_so_and_stops_there():
    with pytest.MonkeyPatch.context() as monkeypatch:
        mount = _mount(monkeypatch, is_connected=False, ascom=Failed)

        assert mount.reachable is False
        assert mount.why_not_operational == mount.why_not_reachable


def test_enable_axes_touches_only_the_disabled_axis():
    with pytest.MonkeyPatch.context() as monkeypatch:
        mount = _mount(monkeypatch, axes_enabled=False)
        mount.pw.status().mount.axis0.is_enabled = True

        mount.enable_axes()

        assert "mount_enable(1,)" in mount.pw.calls
        assert "mount_enable(0,)" not in mount.pw.calls


def test_park_refuses_a_de_energized_mount_instead_of_commanding_it():
    with pytest.MonkeyPatch.context() as monkeypatch:
        mount = _mount(monkeypatch, axes_enabled=False)

        response = mount.park()

        assert response.errors
        assert "axes are not enabled" in response.errors[0]
        assert "mount_park" not in mount.pw.calls


def test_start_tracking_refuses_a_de_energized_mount():
    with pytest.MonkeyPatch.context() as monkeypatch:
        mount = _mount(monkeypatch, axes_enabled=False)

        response = mount.start_tracking()

        assert response.errors
        assert "axes are not enabled" in response.errors[0]
        assert "mount_tracking_on" not in mount.pw.calls


def test_a_settle_wait_on_an_unreachable_mount_fails_rather_than_claiming_settled():
    """It answered True from a path that never looked at the mount."""
    module = _mount_module()
    with pytest.MonkeyPatch.context() as monkeypatch:
        mount = _mount(monkeypatch, is_connected=False, ascom=Failed)

        assert mount.wait_until_settled(module.SettleMode.SLEW) is False


def _focuser(monkeypatch, *, on=True, exists=True, is_connected=True, is_enabled=False, position=1000.0):
    module = _focuser_module()

    class FakeFocuser(module.Focuser):
        def is_on(self) -> bool:
            return on

    focuser = object.__new__(FakeFocuser)
    focuser.pw = FakePwi4(
        FakeStatus(
            focuser=FakeFocuserSection(exists=exists, is_connected=is_connected, is_enabled=is_enabled, position=position)
        )
    )
    return focuser


def test_the_focuser_deploys_by_being_energized_not_by_where_it_sits():
    """No position may be assumed or required: the hardware's previous state is unknown."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        idle = _focuser(monkeypatch, is_enabled=False, position=12345.0)
        assert idle.reachable is True
        assert idle.deployed is False

        energized = _focuser(monkeypatch, is_enabled=True, position=12345.0)
        assert energized.deployed is True
        assert energized.operational is True


def test_the_focuser_connect_does_not_tear_down_pwi4_when_ascom_fails():
    """A failed ASCOM read used to disconnect and de-energize a working PWI4 connection (#173)."""
    module = _focuser_module()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(module, "ascom_run", lambda *_args, **_kwargs: Failed())
        focuser = _focuser(monkeypatch, is_enabled=True)

        response = focuser.connect()

        assert response.errors
        assert "could not read ASCOM Connected" in response.errors[0]
        assert "focuser_disconnect" not in focuser.pw.calls
        assert "focuser_disable" not in focuser.pw.calls
