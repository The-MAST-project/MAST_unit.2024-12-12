"""The guide log: a nightly JSONL file, one record per line, flushed as it is written, carrying the
session id and the validation event kinds. Pure stdlib I/O -- no hardware, no share."""

from __future__ import annotations

import pytest

from mast_guider.guide_log import (
    ALERT,
    CYCLE,
    EVENT,
    REFERENCE_SHIFT,
    SESSION_BEGIN,
    SESSION_END,
    VALIDATION,
    GuideLog,
    read_guide_log,
)


def test_a_session_writes_begin_cycles_and_end_in_order(tmp_path):
    path = tmp_path / "night.jsonl"
    log = GuideLog(path, "sess-1", config={"exposure": 3.0}, guide_stars={"n": 14})
    log.cycle(
        decision="applied", dx=5.0, dy=-3.0, error_arcsec=(2.6, -1.5),
        correction_arcsec=(1.8, -1.0), scatter_px=0.2, n_used=14,
    )
    log.cycle(decision="deadband", dx=0.1, dy=0.0, error_arcsec=(0.05, 0.0), scatter_px=0.2, n_used=14)
    log.end(reason="stopped")

    records = read_guide_log(path)
    assert [r["type"] for r in records] == [SESSION_BEGIN, CYCLE, CYCLE, SESSION_END]
    assert all(r["session_id"] == "sess-1" for r in records)
    assert all("ts" in r for r in records)

    begin = records[0]
    assert begin["config"] == {"exposure": 3.0} and begin["guide_stars"] == {"n": 14}

    applied = records[1]
    assert applied["decision"] == "applied"
    assert applied["correction_arcsec"] == [1.8, -1.0]  # tuple serialized as a list
    assert records[2]["correction_arcsec"] is None  # deadband sent no correction

    assert records[3]["reason"] == "stopped"


def test_validation_events_share_the_log(tmp_path):
    path = tmp_path / "night.jsonl"
    with GuideLog(path, "sess-2") as log:
        log.cycle(decision="applied", dx=1.0, dy=1.0)
        log.event(VALIDATION, solved=True, residual_arcsec=0.4)
        log.event(REFERENCE_SHIFT, shift_arcsec=3.1)
        log.event(ALERT, reason="out_of_position")

    records = read_guide_log(path)
    events = [r for r in records if r["type"] == EVENT]
    assert [e["event"] for e in events] == [VALIDATION, REFERENCE_SHIFT, ALERT]
    assert events[0]["residual_arcsec"] == 0.4
    assert events[2]["reason"] == "out_of_position"
    assert records[-1]["type"] == SESSION_END  # the context manager closed the session


def test_each_line_is_flushed_so_a_reader_can_follow(tmp_path):
    path = tmp_path / "night.jsonl"
    log = GuideLog(path, "sess-3")
    log.cycle(decision="applied", dx=1.0, dy=1.0)
    # Not ended yet: a tailing reader must already see both lines on disk.
    records = read_guide_log(path)
    assert [r["type"] for r in records] == [SESSION_BEGIN, CYCLE]
    log.end()


def test_a_torn_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "night.jsonl"
    log = GuideLog(path, "sess-4")
    log.cycle(decision="applied", dx=1.0, dy=1.0)
    log.end()
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"type": "cycle", "session_id": "sess-4"')  # a crash mid-write, no newline

    records = read_guide_log(path)  # must not raise
    assert [r["type"] for r in records] == [SESSION_BEGIN, CYCLE, SESSION_END]


def test_writing_after_end_raises(tmp_path):
    log = GuideLog(tmp_path / "night.jsonl", "sess-5")
    log.end()
    with pytest.raises(RuntimeError):
        log.cycle(decision="applied", dx=1.0, dy=1.0)


def test_end_is_idempotent(tmp_path):
    log = GuideLog(tmp_path / "night.jsonl", "sess-6")
    log.end(reason="first")
    log.end(reason="second")  # must not raise or write a second end
    assert [r["type"] for r in read_guide_log(tmp_path / "night.jsonl")] == [SESSION_BEGIN, SESSION_END]


def test_ship_to_copies_the_night_file(tmp_path):
    local = tmp_path / "ram" / "night.jsonl"  # a RAM-disk path, created on open
    log = GuideLog(local, "sess-7")
    log.cycle(decision="applied", dx=1.0, dy=1.0)
    log.end()

    share = tmp_path / "share"  # the destination on the share, created by ship_to
    shipped = log.ship_to(share)
    assert shipped.exists() and shipped.name == "night.jsonl"
    assert [r["type"] for r in read_guide_log(shipped)] == [SESSION_BEGIN, CYCLE, SESSION_END]
