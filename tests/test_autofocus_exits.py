"""An analyser that cannot be reached is one that did not start (#291).

With ps3cli down, `PS3CLIClient.connect` raises a bare `Exception`, which the autofocus
run did not catch: on mast01 on 2026-10-05 the thread died on it.
"""

from __future__ import annotations

import socket

import pytest

import focus_analysis
from focus_analysis import FocusAnalysisError


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestAnAnalyserThatIsNotRunning:
    def test_is_an_analyser_that_did_not_start(self):
        with pytest.raises(FocusAnalysisError) as raised:
            focus_analysis.analyze_focus_files(["FOCUS25000.fits"], timeout=1, port=_closed_port())

        assert raised.value.phase == "start"
