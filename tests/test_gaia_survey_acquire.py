"""The Gaia survey's ``acquire`` step, against a fake unit: no hardware, no network, no real waiting."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gaia_accuracy_survey_acquire", Path(__file__).resolve().parent.parent / "tools" / "gaia_accuracy_survey.py"
)
survey = importlib.util.module_from_spec(_SPEC)
sys.modules["gaia_accuracy_survey_acquire"] = survey
_SPEC.loader.exec_module(survey)  # type: ignore[union-attr]


class FakeUnit:
    """Accepts a request, runs it for ``polls`` status reads, then reports it ended."""

    def __init__(self, outcomes=None, polls=2, refuse=()):
        self.outcomes = list(outcomes or [])
        self.polls = polls
        self.refuse = set(refuse)
        self.requests: list[dict] = []
        self._left = 0
        self._latest: dict | None = None
        self._n = 0

    def status(self) -> dict:
        if self._latest and not self._latest.get("ended"):
            self._left -= 1
            if self._left <= 0:
                ok = self.outcomes.pop(0) if self.outcomes else True
                self._latest.update(
                    ended="2026-11-24T20:05:00+00:00",
                    succeeded=ok,
                    state="ended" if ok else "failed",
                    errors=[] if ok else ["spec did not reach tolerance"],
                )
        return {
            "latest": {"acquisition": self._latest} if self._latest else {},
            "mount": {"ra_j2000_hours": 1.5, "dec_j2000_degs": 40.0},
        }

    def start_acquisition(self, params):
        self.requests.append(params)
        if len(self.requests) in self.refuse:
            return False, ["the unit is busy"]
        self._n += 1
        self._left = self.polls
        self._latest = {
            "started": f"2026-11-24T20:0{self._n}:00+00:00",
            "ended": None,
            "succeeded": None,
            "phases": {"spec": {"steps": 2, "last_ra_delta": 0.3, "last_dec_delta": -0.2}},
            "shared_products_path": f"//control/mast-share/MAST/mast02/acq{self._n}",
            "local_products_path": f"D:/acq{self._n}",
            "solver": "mastrometry",
            "errors": [],
        }
        return True, []


TARGETS = [
    {"name": "T01", "ra_deg": "150.0", "dec_deg": "30.0", "g": "9.5"},
    {"name": "T02", "ra_deg": "30.0", "dec_deg": "-5.0", "g": "10.1"},
    {"name": "T03", "ra_deg": "270.0", "dec_deg": "60.0", "g": "8.8"},
]


def _run(unit, out, targets=TARGETS, **kw):
    kw.setdefault("unit_name", "mast02")
    kw.setdefault("mode", "selective")
    return survey.acquire(unit, targets, out, sleep=lambda s: None, log=lambda m: None, **kw)


def _sidecar(out, name):
    return json.loads((out / name / "acquire.json").read_text())


def test_the_request_is_the_gaia_position_and_leaves_the_unit_unguided():
    request = survey.acquisition_request(TARGETS[0], 5.0)
    assert request == {
        "seconds": 5.0,
        "ra_j2000_hours": 10.0,
        "dec_j2000_degs": 30.0,
        "handover_automatically_to_guider": False,
    }


def test_each_target_is_acquired_in_order_and_recorded(tmp_path):
    unit = FakeUnit()
    done = _run(unit, tmp_path)
    assert [d["outcome"] for d in done] == ["succeeded"] * 3
    assert [r["ra_j2000_hours"] for r in unit.requests] == [10.0, 2.0, 18.0]
    s = _sidecar(tmp_path, "T02")
    assert s["target"]["g"] == "10.1"
    assert (s["unit"], s["mode"]) == ("mast02", "selective")
    assert s["phases"]["spec"]["last_ra_delta"] == 0.3
    assert s["shared_products_path"].endswith("acq2")  # T02's own run, not T01's
    assert s["mount_ra_j2000_hours"] == 1.5


def test_a_failed_acquisition_is_recorded_and_the_run_goes_on(tmp_path):
    unit = FakeUnit(outcomes=[True, False, True])
    done = _run(unit, tmp_path)
    assert [d["outcome"] for d in done] == ["succeeded", "failed", "succeeded"]
    assert _sidecar(tmp_path, "T02")["errors"] == ["spec did not reach tolerance"]


def test_a_refusal_is_recorded_and_the_run_goes_on(tmp_path):
    unit = FakeUnit(refuse={1})
    done = _run(unit, tmp_path)
    assert [d["outcome"] for d in done] == ["refused", "succeeded", "succeeded"]
    assert _sidecar(tmp_path, "T01")["errors"] == ["the unit is busy"]


def test_a_run_that_never_ends_times_out(tmp_path):
    unit = FakeUnit(polls=10**6)
    ticks = iter(range(0, 10**6, 100))
    done = survey.acquire(
        unit,
        TARGETS[:1],
        tmp_path,
        unit_name="mast02",
        mode="selective",
        timeout=250,
        clock=lambda: next(ticks),
        sleep=lambda s: None,
        log=lambda m: None,
    )
    assert done[0]["outcome"] == "timeout"
    assert _sidecar(tmp_path, "T01")["outcome"] == "timeout"


def test_a_target_already_acquired_is_skipped_until_redo(tmp_path):
    _run(FakeUnit(), tmp_path)
    again = FakeUnit()
    assert _run(again, tmp_path) == []
    assert again.requests == []
    assert len(_run(again, tmp_path, redo=True)) == 3


def test_the_previous_runs_record_is_not_mistaken_for_ours(tmp_path):
    unit = FakeUnit()
    _run(unit, tmp_path, targets=TARGETS[:1])  # leaves an ended record behind
    unit.polls = 3
    done = _run(unit, tmp_path, targets=TARGETS[1:2])
    assert done[0]["shared_products_path"].endswith("acq2")


def test_the_command_line(tmp_path, capsys):
    csv_path = tmp_path / "targets.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(TARGETS[0]))
        w.writeheader()
        w.writerows(TARGETS)
    unit = FakeUnit(outcomes=[True, False])
    rc = survey.main(
        [
            "acquire",
            str(csv_path),
            "--unit",
            "mast02",
            "--mode",
            "selective",
            "--out",
            str(tmp_path / "run"),
            "--only",
            "T01,T02",
        ],
        unit=unit,
    )
    assert rc == 1  # T02 failed
    out = capsys.readouterr().out
    assert "2 run, 1 succeeded" in out and "not succeeded: T02" in out
    assert len(unit.requests) == 2


def test_an_unknown_target_name_is_refused(tmp_path):
    csv_path = tmp_path / "targets.csv"
    csv_path.write_text("name,ra_deg,dec_deg,g\nT01,1,2,9\n")
    with pytest.raises(SystemExit, match="T09"):
        survey.read_targets(csv_path, ["T09"])
