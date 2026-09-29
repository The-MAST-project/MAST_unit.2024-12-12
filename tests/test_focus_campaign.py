"""The convergence campaign's decisions, pinned without sky, hardware or a service.

Everything here is about the parts that decide whether a night's data is usable:
the order offsets are attempted in, that a run already paid for is never repeated,
and that the summary answers the three questions the campaign exists to ask.
"""

from __future__ import annotations

import json

import pytest

from common.config.calibration import FocusConvergenceCampaignSettings as Settings

pytest.importorskip("calibration.campaign")
from calibration.campaign import (  # noqa: E402
    RunRecord,
    append_record,
    campaign_folder,
    load_done,
    plan_offsets,
    summarise,
)


def _rec(**kw):
    base = dict(
        index=0,
        offset=500,
        seed=12531,
        reference=12031,
        started_utc="2026-10-01T20:00:00+00:00",
        duration_seconds=120.0,
        converged=True,
        best_position=12033.0,
        error_ticks=2.0,
    )
    base.update(kw)
    return RunRecord(**base)


class TestTheOrderOffsetsAreAttempted:
    def test_signs_alternate_so_a_short_night_still_carries_both(self):
        """A night cut short must still allow the inward/outward comparison.

        Doing one whole sign first and the other after means losing half a night
        loses one sign entirely -- and the asymmetry question with it, which is the
        one thing a single-sided sweep can never answer.
        """
        order = plan_offsets(Settings(offsets=[500, 2000, 5000]))
        assert order == [500, -500, 2000, -2000, 5000, -5000]

        # every prefix that reaches a magnitude carries BOTH signs of it
        for stop in range(2, len(order) + 1, 2):
            seen = order[:stop]
            for magnitude in {abs(o) for o in seen}:
                assert magnitude in seen and -magnitude in seen

    def test_magnitudes_escalate_so_the_cheap_cases_run_first(self):
        order = plan_offsets(Settings(offsets=[500, 2000, 10000]))
        assert [abs(o) for o in order] == sorted(abs(o) for o in order)

    def test_sequential_mode_groups_by_sign(self):
        order = plan_offsets(Settings(offsets=[500, 2000], interleave=False))
        assert order == [500, 2000, -500, -2000]

    def test_repeats_multiply_every_offset(self):
        order = plan_offsets(Settings(offsets=[500], repeats=3))
        assert order == [500, 500, 500, -500, -500, -500] or order.count(500) == 3


class TestSkyTimeIsNeverPaidTwice:
    def test_a_finished_run_is_recorded_before_the_next_one_starts(self, tmp_path):
        """Append-per-run, not a summary at the end.

        Clouds, an abort or a killed process must not cost the runs already flown.
        """
        append_record(tmp_path, _rec(index=0))
        append_record(tmp_path, _rec(index=1, offset=-500, converged=False, best_position=None, error_ticks=None))
        lines = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["offset"] == 500
        assert json.loads(lines[1])["converged"] is False

    def test_resuming_reads_back_what_was_done(self, tmp_path):
        append_record(tmp_path, _rec(index=0))
        assert len(load_done(tmp_path)) == 1

    def test_a_truncated_last_line_does_not_lose_the_rest(self, tmp_path):
        """A process killed mid-write leaves a partial line; the paid-for runs above
        it are still good, and must survive."""
        append_record(tmp_path, _rec(index=0))
        with open(tmp_path / "runs.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"index": 1, "offset": -50')  # killed here
        done = load_done(tmp_path)
        assert len(done) == 1 and done[0]["index"] == 0

    def test_no_file_yet_is_not_an_error(self, tmp_path):
        assert load_done(tmp_path) == []


class TestTheSummaryAnswersTheThreeQuestions:
    ROWS = [
        dict(offset=500, seed=12531, converged=True, best_position=12033.0, error_ticks=2.0),
        dict(offset=-500, seed=11531, converged=True, best_position=12028.0, error_ticks=-3.0),
        dict(offset=5000, seed=17031, converged=True, best_position=12040.0, error_ticks=9.0),
        dict(offset=-5000, seed=7031, converged=False, best_position=None, error_ticks=None),
    ]

    def test_capture_range_is_reported_per_sign(self):
        out = summarise(self.ROWS)
        assert "outward (+): converged 2/2, capture range >= 5000" in out
        assert "inward  (-): converged 1/2, capture range >= 500" in out

    def test_the_first_failing_offset_is_named(self):
        assert "first failure at 5000" in summarise(self.ROWS)

    def test_accuracy_is_the_spread_of_solved_positions(self):
        out = summarise(self.ROWS)
        assert "3 solved" in out and "spread 12.0" in out

    def test_asymmetry_compares_the_two_sides(self):
        """The differential donut move exists to remove the in/out difference. If the
        two sides land in different places, it is not doing its job -- so the number
        is reported rather than averaged away."""
        assert "asymmetry: outward mean - inward mean = +8.5 ticks" in summarise(self.ROWS)

    def test_asymmetry_is_omitted_when_only_one_side_solved(self):
        one_sided = [r for r in self.ROWS if r["offset"] > 0]
        assert "asymmetry" not in summarise(one_sided)

    def test_no_runs_says_so_rather_than_dividing_by_zero(self):
        assert summarise([]) == "no runs yet"


class TestWhereProductsLand:
    def test_an_explicit_root_is_honoured(self, tmp_path):
        """With the share down but Z: still mapped, `Filer.shared.root` keeps pointing
        at the dead share. The campaign must be pinnable regardless."""
        folder = campaign_folder(Settings(products_root=str(tmp_path)), "2026-10-01")
        assert folder == tmp_path / "Campaigns" / "focus-convergence" / "2026-10-01"
        assert folder.is_dir()

    def test_the_default_probes_rather_than_asking_about_drive_letters(self, monkeypatch, tmp_path):
        import calibration.campaign as mod

        class FakeFiler:
            # what a probe returns when the share is unreachable
            def accessible_shared_root(self):
                return str(tmp_path)

        monkeypatch.setattr(mod, "Filer", FakeFiler)
        folder = campaign_folder(Settings(products_root=None), "2026-10-01")
        assert str(folder).startswith(str(tmp_path))
