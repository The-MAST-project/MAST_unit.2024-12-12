"""The stage controller's electrical readings, and the two drive faults it does not flag.

On mast07 on 2026-11-24 the stage "moved" for 150 s without moving, twice over, with no
controller flag set either time: first with the power stage off (cleared by power-cycling the
controller), then at full winding voltage with no current -- an open motor circuit. The
readings that showed both go into the stage's status, and each fault is logged once per move.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest

try:
    import stage as stage_module
    from common.models.statuses import StageDrive
    from stage import Stage
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

OK = 0
POWER_OFF, POWER_NORMAL = 0x01, 0x03


class FakeXimc:
    """A controller whose chart read returns `chart`, or fails."""

    def __init__(self, chart=None, raises=False):
        self.chart = chart
        self.raises = raises

    def get_chart_data(self, device, ref):
        if self.raises:
            raise OSError("chart read failed")
        if self.chart is None:
            return -1
        for k, v in self.chart.items():
            setattr(ref, k, v)
        return OK


class _Stage(Stage):
    def __del__(self):
        """`Stage.__del__` closes the device through the real ximclib, the fake long gone by then."""


@pytest.fixture
def stage(monkeypatch):
    monkeypatch.setattr(stage_module, "byref", lambda obj: obj)
    monkeypatch.setattr(stage_module, "chart_data_t", SimpleNamespace)
    monkeypatch.setattr(stage_module.Result, "Ok", OK, raising=False)
    s = object.__new__(_Stage)
    s.stage_lock = threading.Lock()
    s.device = 1
    return s


def _status(**kw):
    base = {"Upwr": 1222, "Ipwr": 70, "PWRSts": POWER_NORMAL, "EncPosition": 23}
    return SimpleNamespace(**{**base, **kw})


def _chart(volts=3.65, milliamps=-1, duty=3229):
    return {"WindingVoltageA": round(volts * 100), "WindingCurrentA": milliamps, "DutyCycle": duty}


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


class TestReading:
    def test_the_readings_are_in_physical_units(self, stage, monkeypatch):
        monkeypatch.setattr(stage_module, "ximclib", FakeXimc(_chart(volts=3.65, milliamps=412)), raising=False)
        drive = stage._read_drive(_status())

        assert drive.supply_volts == pytest.approx(12.22), "Upwr is in tens of mV"
        assert drive.supply_milliamps == 70
        assert drive.power_state == "normal"
        assert drive.duty_cycle == 3229
        assert drive.winding_volts == pytest.approx(3.65), "WindingVoltageA is in tens of mV"
        assert drive.winding_milliamps == 412
        assert drive.encoder_position == 23

    def test_an_unknown_power_state_is_shown_raw(self, stage, monkeypatch):
        monkeypatch.setattr(stage_module, "ximclib", FakeXimc(_chart()), raising=False)
        assert stage._read_drive(_status(PWRSts=0x7)).power_state == "0x7"

    @pytest.mark.parametrize("fake", [FakeXimc(None), FakeXimc(raises=True)], ids=["refused", "raised"])
    def test_a_failed_chart_read_leaves_only_the_winding_unknown(self, stage, monkeypatch, fake):
        """It rides on ontimer's status read, which must not fail because of it."""
        monkeypatch.setattr(stage_module, "ximclib", fake, raising=False)
        drive = stage._read_drive(_status())

        assert drive.winding_volts is None and drive.winding_milliamps is None and drive.duty_cycle is None
        assert drive.supply_volts == pytest.approx(12.22), "the status_t readings are still there"

    def test_the_status_carries_the_drive(self):
        """`test_status_fields_are_populated` checks it is passed; this, that it is the model."""
        assert stage_module.StageStatus.model_fields["drive"].annotation == StageDrive | None


def _drive(**kw):
    base = {
        "supply_volts": 12.22,
        "supply_milliamps": 70,
        "power_state": "normal",
        "duty_cycle": 3229,
        "winding_volts": 3.65,
        "winding_milliamps": -1,
        "encoder_position": 2,
    }
    return StageDrive(**{**base, **kw})


class TestFaults:
    def test_full_voltage_with_no_current_is_an_open_circuit(self, stage, caplog):
        """mast07 after the power cycle: 3.65 V on the winding, -1 mA through it."""
        for _ in range(stage_module.DRIVE_FAULT_READS):
            stage._watch_drive(_drive(), running=True)

        errors = _errors(caplog)
        assert len(errors) == 1, errors
        assert "open" in errors[0] and "3.65 V" in errors[0] and "connector" in errors[0]

    def test_a_running_move_with_the_power_stage_off_is_reported(self, stage, caplog):
        """mast07 before the power cycle: the move ran, PWRSts read off, nothing was driven."""
        for _ in range(stage_module.DRIVE_FAULT_READS):
            stage._watch_drive(_drive(power_state="off", winding_volts=1.38, duty_cycle=0), running=True)

        errors = _errors(caplog)
        assert len(errors) == 1, errors
        assert "power stage off" in errors[0] and "power-cycle" in errors[0]

    def test_a_fault_is_reported_once_per_move(self, stage, caplog):
        for _ in range(10):
            stage._watch_drive(_drive(), running=True)
        assert len(_errors(caplog)) == 1, "a 150 s homing must not log it 75 times"

    def test_a_single_read_is_not_a_fault(self, stage, caplog):
        """The first read of a move can catch the drive before current builds up."""
        stage._watch_drive(_drive(), running=True)
        stage._watch_drive(_drive(winding_milliamps=400), running=True)
        stage._watch_drive(_drive(), running=True)
        assert _errors(caplog) == [], "the reads must be consecutive"

    def test_the_next_move_reports_it_again(self, stage, caplog):
        for _ in range(stage_module.DRIVE_FAULT_READS):
            stage._watch_drive(_drive(), running=True)
        stage._watch_drive(_drive(), running=False)
        for _ in range(stage_module.DRIVE_FAULT_READS):
            stage._watch_drive(_drive(), running=True)
        assert len(_errors(caplog)) == 2

    @pytest.mark.parametrize(
        "drive",
        [
            _drive(winding_milliamps=412),
            _drive(winding_volts=0.2),
            _drive(winding_volts=None, winding_milliamps=None),
        ],
        ids=["current flows", "not driven", "no chart read"],
    )
    def test_a_healthy_or_unread_drive_is_not_a_fault(self, stage, caplog, drive):
        for _ in range(5):
            stage._watch_drive(drive, running=True)
        assert _errors(caplog) == []

    def test_nothing_is_reported_without_a_move(self, stage, caplog):
        """At rest the power stage may be off, or the winding held, by design."""
        for _ in range(5):
            stage._watch_drive(_drive(power_state="off"), running=False)
            stage._watch_drive(_drive(), running=False)
        assert _errors(caplog) == []


def test_the_log_line_tolerates_missing_readings():
    line = stage_module.describe_drive(StageDrive(supply_volts=12.22, power_state="off"))
    assert "12.22 V" in line and "power stage off" in line and "?" in line
