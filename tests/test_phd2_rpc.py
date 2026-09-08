"""A PHD2 reply reaches the caller that asked for it, and nobody else (#220).

Every request used to carry `"id": 1`, and the reader thread dropped each reply into one
shared slot and called `notify()` -- which wakes an *arbitrary* waiter. Two callers in flight
and the reply went to whichever thread woke, so the wrong caller returned someone else's
answer, and the rightful one waited on a slot that had already been emptied. Because
`notify()` wakes one waiter and `call` re-checked under `while not self.response`, that
wake-up was gone for good: the caller parked for the life of the process.

Both halves were seen on mast01 on 2026-09-08 -- a temperature reading returned where a bool
was expected, and then `GET /unit/status` ceasing to answer at all.

These drive the real `call`, `_deliver` and `_read_forever` against a fake connection, so
nothing here needs PHD2.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time

import pytest

from phd2.phd2 import PHD2Connector, PHD2ConnectorError

SHORT_TIMEOUT = 0.3


class FakeConnection:
    """The socket half of PHD2Connection: what was written, and what to read next."""

    def __init__(self):
        self.sent: list[str] = []
        self.lines: queue.Queue = queue.Queue()

    def write_line(self, line: str) -> None:
        self.sent.append(line)

    def read_line(self) -> str:
        return self.lines.get()


def make_connector() -> PHD2Connector:
    """A real PHD2Connector with only the RPC machinery built -- __init__ reaches hardware."""
    p = object.__new__(PHD2Connector)
    p.conn = FakeConnection()
    p._rpc_lock = threading.Lock()
    p._send_lock = threading.Lock()
    p._next_request_id = 0
    p._pending = {}
    p._terminate = False
    return p


def sent_ids(connector: PHD2Connector) -> list[int]:
    return [json.loads(line)["id"] for line in connector.conn.sent]


def wait_for_pending(connector: PHD2Connector, count: int, timeout: float = 2.0) -> None:
    """Block until `count` requests are registered, so a test can answer them by id."""
    deadline = time.monotonic() + timeout
    while len(connector._pending) < count:
        assert time.monotonic() < deadline, f"only {len(connector._pending)} of {count} calls reached _pending"
        time.sleep(0.005)


def ask_and_ignore_the_timeout(connector: PHD2Connector, method: str) -> threading.Thread:
    """Fire a call whose reply never comes; the test is about the request, not the answer."""

    def run() -> None:
        with contextlib.suppress(PHD2ConnectorError):
            connector.call(method, timeout=SHORT_TIMEOUT)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


class TestAReplyFindsItsOwnCaller:
    def test_two_concurrent_calls_do_not_cross(self):
        """The failure that was seen: one caller returning the other's answer."""
        connector = make_connector()
        answers: dict[str, object] = {}

        def ask(method: str) -> None:
            answers[method] = connector.call(method, timeout=2.0)["result"]

        first = threading.Thread(target=ask, args=("get_ccd_temperature",))
        second = threading.Thread(target=ask, args=("is_settling",))
        first.start()
        wait_for_pending(connector, 1)
        second.start()
        wait_for_pending(connector, 2)

        ids = sent_ids(connector)
        # Answered out of order, which is what a shared slot could not survive.
        connector._deliver({"jsonrpc": "2.0", "id": ids[1], "result": False})
        connector._deliver({"jsonrpc": "2.0", "id": ids[0], "result": {"temperature": 4.9}})

        first.join(timeout=3)
        second.join(timeout=3)

        assert answers["get_ccd_temperature"] == {"temperature": 4.9}
        assert answers["is_settling"] is False

    def test_every_request_carries_a_distinct_id(self):
        connector = make_connector()
        threads = [
            ask_and_ignore_the_timeout(connector, method) for method in ("get_app_state", "get_exposure", "get_pixel_scale")
        ]
        wait_for_pending(connector, 3)

        ids = sent_ids(connector)
        for thread in threads:
            thread.join(timeout=2)

        assert len(set(ids)) == len(ids) == 3, f"ids must be distinct, got {ids}"

    def test_a_reply_nobody_is_waiting_for_releases_nobody(self):
        connector = make_connector()

        connector._deliver({"jsonrpc": "2.0", "id": 4242, "result": "stale"})

        with pytest.raises(PHD2ConnectorError, match="no reply"):
            connector.call("get_app_state", timeout=SHORT_TIMEOUT)


class TestNobodyWaitsForever:
    def test_a_call_with_no_reply_raises_rather_than_parking(self):
        connector = make_connector()

        with pytest.raises(PHD2ConnectorError) as caught:
            connector.call("get_app_state", timeout=SHORT_TIMEOUT)

        assert "get_app_state" in str(caught.value), "the error must name the call that went unanswered"

    def test_the_pending_entry_is_dropped_when_a_call_times_out(self):
        """Otherwise a run of unanswered calls leaks a slot each."""
        connector = make_connector()

        with pytest.raises(PHD2ConnectorError):
            connector.call("get_app_state", timeout=SHORT_TIMEOUT)

        assert connector._pending == {}

    def test_a_reader_that_exits_releases_every_waiting_caller(self):
        """The connection going away is the other way a caller waits for nothing."""
        connector = make_connector()
        failures: list[Exception] = []

        def ask() -> None:
            try:
                connector.call("get_app_state", timeout=5.0)
            except Exception as ex:  # noqa: BLE001 -- the exception IS the assertion
                failures.append(ex)

        reader = threading.Thread(target=connector._worker)
        reader.start()
        caller = threading.Thread(target=ask)
        caller.start()
        wait_for_pending(connector, 1)

        connector.conn.lines.put("")  # server disconnected
        reader.join(timeout=3)
        caller.join(timeout=3)

        assert len(failures) == 1
        assert isinstance(failures[0], PHD2ConnectorError)
        assert "no reply" in str(failures[0])


class TestOneWriterAtATime:
    """`write_line` loops on `socket.send`, so two callers writing at once can hand PHD2 a
    spliced line. Nothing about the id fixes that -- it needs the send serialized."""

    def test_the_send_happens_under_the_lock(self):
        connector = make_connector()
        held: list[bool] = []

        def observe(line: str) -> None:
            held.append(connector._send_lock.locked())

        connector.conn.write_line = observe  # type: ignore[method-assign]

        with pytest.raises(PHD2ConnectorError):
            connector.call("get_app_state", timeout=SHORT_TIMEOUT)

        assert held == [True]
