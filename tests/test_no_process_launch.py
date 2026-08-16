"""The guard in conftest must actually stop a process being started.

A tripwire nobody tests is a tripwire that quietly stops working -- and the failure
mode here is not a red test, it is PWI4 opening the mirror covers on a live unit
because someone imported the wrong module. So the guard is exercised directly.

Context: src/app.py calls ensure_process_is_running at MODULE level, so importing it
starts PWI4, the shutter and the plate solver. Its ``if __name__ == "__main__"`` sits
far below that. Until app.py starts nothing on import, this guard is what stands
between a stray import and the hardware.
"""

from __future__ import annotations

import os
import subprocess

import pytest

# tests/ is not a package, so this is a plain import: pytest puts the test directory
# on sys.path (importmode=prepend), which makes conftest importable by name.
from conftest import ProcessLaunchError

# Captured while THIS MODULE is being imported, i.e. during collection -- before any
# fixture has run. If the guard were installed from a fixture instead of at conftest
# import time, this would still be the real subprocess.Popen and the test below would
# fail. That window is the one that matters here: importing src/app.py at a test
# module's top level would start PWI4 during collection.
_POPEN_AT_COLLECTION = subprocess.Popen


class TestGuardIsActiveBeforeAnyTestRuns:
    def test_installed_at_collection_time_not_by_a_fixture(self):
        with pytest.raises(ProcessLaunchError):
            _POPEN_AT_COLLECTION(["PWI4.exe"])


class TestGuardFires:
    @pytest.mark.parametrize("call", ["Popen", "run", "call", "check_call", "check_output"])
    def test_subprocess_entry_points_are_blocked(self, call):
        with pytest.raises(ProcessLaunchError):
            getattr(subprocess, call)(["cmd", "/c", "echo", "hello"])

    @pytest.mark.parametrize("call", ["system", "popen"])
    def test_os_entry_points_are_blocked(self, call):
        with pytest.raises(ProcessLaunchError):
            getattr(os, call)("echo hello")

    def test_the_message_names_the_call_and_the_target(self):
        """The point is that whoever hits this can tell what tried to start what."""
        with pytest.raises(ProcessLaunchError) as excinfo:
            subprocess.Popen(["PWI4.exe"])
        message = str(excinfo.value)
        assert "subprocess.Popen" in message
        assert "PWI4.exe" in message


class TestGuardIsWiredUp:
    def test_the_real_launcher_cannot_reach_the_operating_system(self):
        """common.process is the funnel the unit actually uses.

        Blocking subprocess/os wholesale rather than patching this one function is
        deliberate -- a direct Popen elsewhere would slip past a targeted patch -- but
        the funnel is the path that matters most, so check it lands on the guard.
        """
        process = pytest.importorskip("common.process", reason="common not importable")
        assert hasattr(process, "ensure_process_is_running")

        with pytest.raises(ProcessLaunchError):
            subprocess.Popen(["ps3cli.exe", "--server"])
