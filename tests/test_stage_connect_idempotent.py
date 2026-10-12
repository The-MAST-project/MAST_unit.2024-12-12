"""Setting the stage's `connected` to the state it is already in does nothing.

`open_device` on an already-open stage lost the device: the stage read disconnected, its
`ontimer` returned early, and an `Aborting` raised by the shutdown could never end -- the
shutdown waited out its whole 120 s abort budget (mast00, 2026-10-12).

The stage is never constructed here (that enumerates USB ports and opens the controller); the
setter runs on a bare instance over a recording ximclib.
"""

from __future__ import annotations

import pytest

import stage as stage_module
from stage import Stage


class RecordingXimc:
    def __init__(self):
        self.calls: list[str] = []

    def open_device(self, uri):
        self.calls.append("open")
        return 7

    def close_device(self, _handle):
        self.calls.append("close")


@pytest.fixture
def ximc(monkeypatch):
    lib = RecordingXimc()
    monkeypatch.setattr(stage_module, "ximclib", lib)
    return lib


class _BareStage(Stage):
    def __del__(self):
        """Not the real one: it closes the device and cancels a timer this instance never had."""


def _stage(device: int) -> Stage:
    stage = object.__new__(_BareStage)
    stage.device = device
    stage.device_uri = b"xi-com:///COM11"
    stage.is_on = lambda: True
    return stage


def test_connecting_an_open_stage_does_not_reopen_it(ximc):
    stage = _stage(device=7)

    stage.connected = True

    assert ximc.calls == []
    assert stage.connected is True


def test_disconnecting_a_closed_stage_does_nothing(ximc):
    stage = _stage(device=-1)

    stage.connected = False

    assert ximc.calls == []


def test_a_real_change_still_opens_and_closes(ximc):
    stage = _stage(device=-1)

    stage.connected = True
    assert ximc.calls == ["open"]
    assert stage.connected is True

    stage.connected = False
    assert ximc.calls == ["open", "close"]
    assert stage.connected is False
