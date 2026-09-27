"""The guide camera's USB link is found once a session and logged, never enforced (#264).

On mast01 and mast04 the camera enumerates behind two cascaded USB 2.0 hubs and reads a
full frame out in 5.7 s; on mast02, on a SuperSpeed hub, it takes 0.87 s. The camera works
either way, so nothing in the log told the slow unit apart. PHD2 holds the camera, so the
SDK's `IsUSB3Host` cannot be asked; the PnP parent chain can, without touching the device.

The verdict line is a contract with whatever scrapes the logs for it, so its shape is pinned
here. The chains below are the ones the production walk returned on 2026-09-24.
"""

from __future__ import annotations

import logging
import re
import subprocess

import pytest

from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from imagers import Imager, usb_link
from imagers.usb_link import UsbLink, classify, log_usb_link, read_parent_chain

MAST01_CHAIN = [
    "Generic USB Hub | USB2.0 Hub",
    "Generic USB Hub | USB2.0 Hub",
    "USB Root Hub (USB 3.0) |",
]
MAST03_CHAIN = [
    "Generic SuperSpeed USB Hub | USB3.0 Hub",
    "USB Root Hub (USB 3.0) |",
]

#: What a scraper keys on. Changing the line means changing this, which is the point.
VERDICT = re.compile(r'^(?:DEGRADED )?usb-link=(SuperSpeed|HighSpeed|unknown) chain="([^"]*)"')


# --- classify ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chain", "expected"),
    [
        (MAST01_CHAIN, UsbLink.HighSpeed),
        (MAST03_CHAIN, UsbLink.SuperSpeed),
        (None, UsbLink.Unknown),
        ([], UsbLink.Unknown),
        (["USB Root Hub (USB 3.0) |"], UsbLink.Unknown),
    ],
    ids=["mast01", "mast03", "no-camera", "empty", "root-port-only"],
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
    """Trailing padding is PowerShell's, and real: the bus description comes back space-filled."""
    monkeypatch.setattr(usb_link.sys, "platform", "win32")
    padded = "\r\n".join(hop + "             " for hop in MAST01_CHAIN) + "\r\n"
    monkeypatch.setattr(usb_link.subprocess, "run", lambda *a, **k: _completed(padded))
    assert read_parent_chain() == MAST01_CHAIN


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


# --- log_usb_link: the line a scraper reads ---------------------------------------------


def _verdicts(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if VERDICT.match(r.getMessage())]


def _log_with(monkeypatch, caplog, chain) -> UsbLink:
    monkeypatch.setattr(usb_link, "read_parent_chain", lambda: chain)
    with caplog.at_level(logging.INFO):
        return log_usb_link()


def test_a_usb2_path_is_one_degraded_warning_naming_the_chain(monkeypatch, caplog):
    assert _log_with(monkeypatch, caplog, MAST01_CHAIN) is UsbLink.HighSpeed

    (record,) = _verdicts(caplog)
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert message.startswith("DEGRADED usb-link=HighSpeed ")
    match = VERDICT.match(message)
    assert match is not None
    assert match.group(2) == " > ".join(MAST01_CHAIN)


@pytest.mark.parametrize(
    ("chain", "link"),
    [(MAST03_CHAIN, UsbLink.SuperSpeed), (None, UsbLink.Unknown)],
    ids=["superspeed", "unknown"],
)
def test_otherwise_one_info_verdict_and_nothing_degraded(monkeypatch, caplog, chain, link):
    """Logged every session, not only when degraded: the latest line per unit is its state."""
    assert _log_with(monkeypatch, caplog, chain) is link

    (record,) = _verdicts(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage().startswith(f"usb-link={link} ")
    assert not [r for r in caplog.records if "DEGRADED" in r.getMessage()]


# --- Imager.startup ---------------------------------------------------------------------


class _Backend:
    """Records when it was started, so the test can see the walk ran first."""

    def __init__(self, response, caplog):
        self.response = response
        self.caplog = caplog
        self.verdicts_before_startup: int | None = None

    def startup(self):
        self.verdicts_before_startup = len(_verdicts(self.caplog))
        return self.response


@pytest.mark.parametrize(
    "response", [CanonicalResponse_Ok, CanonicalResponse(errors=["backend refused"])], ids=["ok", "refused"]
)
def test_the_imager_logs_the_link_then_answers_as_its_backend(monkeypatch, caplog, response):
    """Whatever backend holds the camera: the walk reads the PnP tree, not the device."""
    backend = _Backend(response, caplog)
    imager = object.__new__(Imager)
    imager._backend = backend
    monkeypatch.setattr(usb_link, "read_parent_chain", lambda: MAST01_CHAIN)

    with caplog.at_level(logging.INFO):
        answer = imager.startup()

    assert answer is response, "a slow link is a slower night, not a failed start"
    assert backend.verdicts_before_startup == 1
    assert len(_verdicts(caplog)) == 1
