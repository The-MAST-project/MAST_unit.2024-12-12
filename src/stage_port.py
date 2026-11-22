"""Free the stage controller's COM port from XILab before the stage scans for it.

XILab, Standa's utility, opens the XIMC controller's COM port and keeps it: while it does,
libximc can neither enumerate the controller nor open it (mast08, 2026-11-22: 1 device and a
good handle with XILab idle; 0 devices and `open_device -> -1` once it had connected).
`Stage.__init__` scans once and, finding nothing, gives up until the app restarts.

So, before the scan: a port that something holds, with XILab running, is XILab's -- end it, wait
for it to be gone and for the port to come free.  The scan that follows then finds the
controller, and if it does not, the controller really is missing, which is the problem to report.

"Held" is tested by opening the port ourselves: Windows serial ports are exclusive, so a refusal
(access denied) means another process has it open.  Windows cannot say *which* process from
here, so the owner is inferred: XILab is running and the port is held.  A port held with no XILab
running is somebody else's, and is left alone and reported.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from typing import Any

from common.mast_logging import get_logger

logger = get_logger(__name__)

#: XILab's executable, compared in lower case.
XILAB_EXE = "xilab.exe"
#: How long a killed XILab may take to be gone.
KILL_TIMEOUT_SECONDS = 10.0
#: How long the port may take to come free once XILab is gone.
RELEASE_TIMEOUT_SECONDS = 5.0
RELEASE_POLL_SECONDS = 0.5


def port_is_held(port: str) -> bool:
    """True when another process has ``port`` open (the open is refused, access denied).

    A port that is not there at all (no such file) is not held: it is missing, and the scan
    reports that.  A port that opens is closed again at once.
    """
    import serial

    try:
        handle = serial.Serial(port)
    except serial.SerialException as ex:
        text = str(ex)
        return "PermissionError" in text or "Access is denied" in text
    handle.close()
    return False


def find_xilab() -> list[Any]:
    """The running XILab processes."""
    import psutil

    found = []
    for proc in psutil.process_iter(["name"]):
        name = proc.info.get("name") or ""
        if name.lower() == XILAB_EXE:
            found.append(proc)
    return found


def kill_and_wait(procs: Sequence[Any], timeout: float = KILL_TIMEOUT_SECONDS) -> list[Any]:
    """Kill ``procs`` and wait for them to be gone; returns the ones still alive."""
    import psutil

    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess):
            proc.kill()
    _gone, alive = psutil.wait_procs(list(procs), timeout=timeout)
    return list(alive)


def _describe(proc: Any) -> str:
    try:
        started = time.strftime("%H:%M:%S", time.localtime(proc.create_time()))
        return f"pid {proc.pid}, user {proc.username()}, started {started}"
    except Exception:  # noqa: BLE001 -- a description must not stop the kill
        return f"pid {getattr(proc, 'pid', '?')}"


def release_xilab(
    ports: Sequence[str],
    *,
    held: Callable[[str], bool] = port_is_held,
    find: Callable[[], list[Any]] = find_xilab,
    kill: Callable[[Sequence[Any]], list[Any]] = kill_and_wait,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Free each of ``ports`` from XILab; True when none of them is left held.

    Never raises: the stage's scan goes on whatever happens here, and says what it finds.
    """
    try:
        return _release_xilab(ports, held, find, kill, sleep)
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"stage port: could not check for XILab ({ex!r}); scanning anyway")
        return False


def _release_xilab(ports, held, find, kill, sleep) -> bool:
    all_free = True
    for port in ports:
        if not held(port):
            continue
        procs = find()
        if not procs:
            logger.warning(f"stage port: {port} is held and XILab is not running: not touching whatever has it")
            all_free = False
            continue
        for proc in procs:
            logger.warning(f"stage port: {port} is held and XILab is running ({_describe(proc)}): killing it")
        alive = kill(procs)
        if alive:
            logger.error(f"stage port: XILab ({', '.join(_describe(p) for p in alive)}) did not die; {port} stays held")
            all_free = False
            continue
        for _ in range(int(RELEASE_TIMEOUT_SECONDS / RELEASE_POLL_SECONDS)):
            if not held(port):
                break
            sleep(RELEASE_POLL_SECONDS)
        if held(port):
            logger.error(f"stage port: XILab is gone but {port} is still held after {RELEASE_TIMEOUT_SECONDS:g}s")
            all_free = False
        else:
            logger.info(f"stage port: XILab ended, {port} is free")
    return all_free
