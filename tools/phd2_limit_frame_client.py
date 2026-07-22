"""Bench client for the PHD2 guiding limit-frame RPC (issue #51 Tier 3).

Speaks PHD2's newline-delimited JSON-RPC event-server protocol (TCP 4400)
and verifies, against a live PHD2 (simulator profile), that the exact wire
encoding `PHD2Connector.set_limit_frame()` emits lands and reads back:

  rect   -- ``set_limit_frame {"roi": [x, y, w, h]}`` round-trips verbatim
            (both the explicit-config rectangle and the derived guiding ROI)
  reset  -- ``set_limit_frame {"roi": null}`` clears the frame

Together with ``tests/test_limit_frame_guiding.py`` (which pins that the
connector emits exactly these params for each ``phd2.limit_frame`` state)
this exercises the full fix without a deployment.

Exit code 0 on success, 1 on any assertion/protocol failure. No third-party
deps. Derived from the 2026-07-07 bench's phd2_exclude_client.py.

Usage:
    python tools/phd2_limit_frame_client.py [--host 127.0.0.1] [--port 4400]
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time

# The three rectangles the connector can send (see tests/test_limit_frame_guiding.py):
EXPLICIT_RECT = [3031, 2692, 2000, 400]  # phd2.limit_frame configured rectangle
DERIVED_ROI = [1144, 822, 6000, 4000]    # fiber/margin-derived guiding ROI


class Phd2Rpc:
    def __init__(self, host: str, port: int, timeout: float = 15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        self._id = 0

    def _readline(self) -> dict:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("PHD2 closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8").strip())

    def call(self, method: str, params=None):
        self._id += 1
        req = {"method": method, "id": self._id}
        if params is not None:
            req["params"] = params
        self.sock.sendall((json.dumps(req) + "\r\n").encode("utf-8"))
        deadline = time.time() + 15.0
        while time.time() < deadline:
            msg = self._readline()
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method} -> error {msg['error']}")
                return msg.get("result")
        raise TimeoutError(f"no response to {method}")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _get_limit_frame(rpc: Phd2Rpc):
    res = rpc.call("get_limit_frame")
    if isinstance(res, dict) and "roi" in res:
        return res["roi"]
    return res


def _check_roundtrip(rpc: Phd2Rpc, label: str, rect: list[int]) -> None:
    rpc.call("set_limit_frame", {"roi": rect})
    got = _get_limit_frame(rpc)
    print(f"[{label}] set {rect} -> read back {got}")
    assert got is not None, f"{label}: limit frame did not take"
    assert list(got) == rect, f"{label}: round-trip mismatch: {got} != {rect}"


def run(host: str, port: int) -> int:
    rpc = Phd2Rpc(host, port)
    try:
        state = rpc.call("get_app_state")
        print(f"connected, app_state={state}")

        try:
            rpc.call("set_connected", {"connected": True})
            time.sleep(2)
        except RuntimeError as ex:
            print(f"set_connected note: {ex}")

        initial = _get_limit_frame(rpc)
        print(f"initial limit frame: {initial}")

        _check_roundtrip(rpc, "explicit-rect", EXPLICIT_RECT)
        _check_roundtrip(rpc, "derived-roi", DERIVED_ROI)

        rpc.call("set_limit_frame", {"roi": None})
        got = _get_limit_frame(rpc)
        print(f"[reset] set None -> read back {got}")
        assert got is None, f"reset failed, still {got}"

        print("PASS -- all three connector encodings land and read back")
        return 0
    finally:
        rpc.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4400)
    args = ap.parse_args()
    try:
        return run(args.host, args.port)
    except AssertionError as ex:
        print(f"FAIL: {ex}")
        return 1
    except Exception as ex:  # noqa: BLE001 - bench script, surface everything
        print(f"ERROR: {type(ex).__name__}: {ex}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
