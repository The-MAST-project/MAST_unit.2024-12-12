"""The guide camera's USB link is found once a session and reported, never enforced (#264).

On mast01 and mast04 the camera enumerates behind two cascaded USB 2.0 hubs and reads a
full frame out in 5.7 s; on mast02, on a SuperSpeed hub, it takes 0.87 s. The camera works
either way, so nothing in the log or in status told the slow unit apart. PHD2 holds the
camera, so the SDK's `IsUSB3Host` cannot be asked; the PnP parent chain can, without
touching the device.

The chains below are the ones measured on 2026-09-23.
"""

from __future__ import annotations

import ast
import logging
import subprocess
from pathlib import Path

import pytest

from common.canonical import CanonicalResponse_Ok
from common.models.statuses import UsbLink
from phd2 import usb_link
from phd2.phd2 import PHD2Connector
from phd2.usb_link import classify, read_parent_chain

SRC = Path(__file__).resolve().parent.parent / "src"

MAST01_CHAIN = [
    "Generic USB Hub | USB2.0 Hub",
    "Generic USB Hub | USB2.0 Hub",
    "USB Root Hub (USB 3.0) | ",
]
MAST02_CHAIN = [
    "Generic SuperSpeed USB Hub | USB3.0 Hub",
    "USB Root Hub (USB 3.0) | ",
]


# --- classify ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chain", "expected"),
    [
        (MAST01_CHAIN, UsbLink.HighSpeed),
        (MAST02_CHAIN, UsbLink.SuperSpeed),
        (None, UsbLink.Unknown),
        ([], UsbLink.Unknown),
        (["USB Root Hub (USB 3.0) | "], UsbLink.Unknown),
    ],
    ids=["mast01", "mast02", "no-camera", "empty", "root-port-only"],
)
def test_classify(chain, expected):
    assert classify(chain) is expected


def test_a_usb2_hub_anywhere_caps_the_link_even_under_a_usb3_hub():
    """A USB 2.0 hub cannot pass SuperSpeed, whatever sits above it."""
    assert classify(["Hub | USB2.0 Hub", "Hub | USB3.0 Hub"]) is UsbLink.HighSpeed


# --- read_parent_chain ------------------------------------------------------------------


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_off_windows_nothing_is_run(monkeypatch):
    monkeypatch.setattr(usb_link.sys, "platform", "darwin")
    monkeypatch.setattr(usb_link.subprocess, "run", lambda *a, **k: pytest.fail("ran a subprocess"))
    assert read_parent_chain() is None


def test_the_walk_output_becomes_the_chain(monkeypatch):
    monkeypatch.setattr(usb_link.sys, "platform", "win32")
    monkeypatch.setattr(usb_link.subprocess, "run", lambda *a, **k: _completed("\r\n".join(MAST01_CHAIN) + "\r\n"))
    assert read_parent_chain() == [hop.strip() for hop in MAST01_CHAIN]


def test_no_camera_is_none(monkeypatch):
    monkeypatch.setattr(usb_link.sys, "platform", "win32")
    monkeypatch.setattr(usb_link.subprocess, "run", lambda *a, **k: _completed(""))
    assert read_parent_chain() is None


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, "powershell", stderr="boom"),
        subprocess.TimeoutExpired("powershell", 1),
        FileNotFoundError("powershell"),
    ],
    ids=["nonzero-exit", "timeout", "no-powershell"],
)
def test_a_failed_walk_is_logged_and_none(monkeypatch, caplog, failure):
    """A diagnostic must not cost the session: it logs why and reports unknown."""

    def fail(*a, **k):
        raise failure

    monkeypatch.setattr(usb_link.sys, "platform", "win32")
    monkeypatch.setattr(usb_link.subprocess, "run", fail)
    with caplog.at_level(logging.WARNING):
        assert read_parent_chain() is None
    assert any("USB" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


# --- PHD2Connector.startup --------------------------------------------------------------


@pytest.fixture
def connector():
    """Only what `startup()` touches; the rest of `__init__` needs a live PHD2."""
    return object.__new__(PHD2Connector)


def _startup_with(monkeypatch, inst: PHD2Connector, chain):
    import phd2.phd2 as phd2_module

    monkeypatch.setattr(phd2_module, "read_parent_chain", lambda: chain)
    return inst.startup()


def test_a_usb2_path_warns_once_and_starts(monkeypatch, caplog, connector):
    with caplog.at_level(logging.WARNING):
        response = _startup_with(monkeypatch, connector, MAST01_CHAIN)

    assert response is CanonicalResponse_Ok
    assert connector.usb_link is UsbLink.HighSpeed
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "USB2.0 Hub" in warnings[0]


def test_a_superspeed_path_is_silent(monkeypatch, caplog, connector):
    with caplog.at_level(logging.WARNING):
        response = _startup_with(monkeypatch, connector, MAST02_CHAIN)

    assert response is CanonicalResponse_Ok
    assert connector.usb_link is UsbLink.SuperSpeed
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_chain_is_unknown_and_starts(monkeypatch, connector):
    assert _startup_with(monkeypatch, connector, None) is CanonicalResponse_Ok
    assert connector.usb_link is UsbLink.Unknown


def test_before_startup_the_link_is_unknown(connector):
    assert connector.usb_link is UsbLink.Unknown


# --- PHD2Connector.status ---------------------------------------------------------------


def test_status_passes_the_found_link():
    """Static, like test_status_fields_are_populated: `status()` needs a live PHD2."""
    tree = ast.parse((SRC / "phd2" / "phd2.py").read_text(encoding="utf-8"))
    status = next(
        node
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef) and cls.name == "PHD2Connector"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "status"
    )
    call = next(c for c in ast.walk(status) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "PHD2ImagerStatus")
    passed = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    assert passed.get("usb_link") == "self.usb_link"


# --- caveats ----------------------------------------------------------------------------


def test_a_usb2_path_is_a_caveat_naming_the_chain(monkeypatch, connector):
    _startup_with(monkeypatch, connector, MAST01_CHAIN)

    (caveat,) = connector.caveats
    assert "USB 2.0" in caveat
    assert "USB2.0 Hub" in caveat


@pytest.mark.parametrize("chain", [MAST02_CHAIN, None], ids=["superspeed", "unknown"])
def test_otherwise_no_caveat(monkeypatch, connector, chain):
    _startup_with(monkeypatch, connector, chain)
    assert connector.caveats == []


def test_the_imager_forwards_its_backend_s_caveats():
    from types import SimpleNamespace

    from imagers import Imager

    imager = object.__new__(Imager)
    imager._backend = SimpleNamespace(caveats=["slow link"])
    assert imager.caveats == ["slow link"]
