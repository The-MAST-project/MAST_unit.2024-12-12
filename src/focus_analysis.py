"""
The pure ps3cli focus-analysis step, lifted out of
``Autofocuser.do_start_autofocus`` so it has no unit/hardware dependencies.

A set of ``FOCUSnnnnn.fits`` images (the focuser position is encoded in each
file name) is handed to a running ``ps3cli --server`` instance, which fits a
v-curve and returns the best focus position. Because the position comes from
the file names, the focuser hardware is not in the loop during analysis -- so
this same code can be driven by the live autofocus routine OR by a replay
harness over a bundle of previously-captured FITS files (see
``tests/autofocus/validate_autofocus_solve.py``).
"""

import datetime
import logging
import time
from pathlib import Path

from common.extended_basemodel import ExtendedBaseModel
from common.mast_logging import init_log
from PlaneWave.ps3cli_client import PS3CLIClient

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)


class PS3FocusSample(ExtendedBaseModel):
    is_valid: bool
    focus_position: float | None = None
    num_stars: int | None = None
    star_rms_diameter_pixels: float | None = None
    vcurve_star_rms_diameter_pixels: float | None = None


class PS3FocusAnalysisResult(ExtendedBaseModel):
    has_solution: bool
    best_focus_position: float | None
    best_focus_star_diameter: float | None
    tolerance: float | None
    vcurve_a: float | None
    vcurve_b: float | None
    vcurve_c: float | None
    focus_samples: list[PS3FocusSample] | None = []
    errors: list[str] | None = []


class PS3AutofocusStatus(ExtendedBaseModel):
    is_running: bool
    last_log_message: str | None = None
    errors: list[str] | None = None
    analysis_result: PS3FocusAnalysisResult | None = None


class FocusAnalysisError(Exception):
    """
    Raised when the ps3cli focus analyser does not start or finish in time.

    ``phase`` is ``"start"`` (the analyser never began running) or ``"finish"``
    (it began but did not complete) so callers can react differently -- the live
    routine aborts the whole run on "start" but retries on "finish".
    """

    def __init__(self, message: str, phase: str):
        super().__init__(message)
        self.phase = phase


def analyze_focus_files(
    files: list[str],
    timeout: float = 60,
    host: str = "127.0.0.1",
    port: int = 8998,
) -> PS3AutofocusStatus:
    """
    Send ``files`` to a running ``ps3cli --server`` focus analyser and return
    its final status.

    Parameters
    ----------
    files
        Paths to the ``FOCUSnnnnn.fits`` images to analyse. Converted to POSIX
        form before being sent, matching the live routine.
    timeout
        Seconds to wait for the analyser to both start and finish.
    host, port
        Where the ps3cli server is listening (defaults to the local server
        started by ``app.py``).

    Raises
    ------
    FocusAnalysisError
        ``phase="start"`` if the analyser never starts, ``phase="finish"`` if it
        starts but does not finish, within ``timeout`` seconds.
    """
    op = "analyze_focus_files"
    ps3_client = PS3CLIClient()
    ps3_client.connect(host, port)
    try:
        posix_files = [Path(file).as_posix() for file in files]
        logger.info(f"calling ps3_client.begin_analyze_focus({posix_files})")
        ps3_client.begin_analyze_focus(posix_files)

        start = datetime.datetime.now()
        end = start + datetime.timedelta(seconds=timeout)

        # wait for the autofocus analyser to start running
        status: PS3AutofocusStatus | None = None
        while datetime.datetime.now() < end:
            d = ps3_client.focus_status()
            if d is None:
                time.sleep(0.1)
                continue
            try:
                status = PS3AutofocusStatus(**d)
            except Exception as ex:
                logger.error(f"{op}: cannot parse focus_status() response: {d=} ({ex=})")
                continue
            if not status.is_running:
                time.sleep(0.1)
            else:
                break

        if datetime.datetime.now() >= end:
            raise FocusAnalysisError(
                f"autofocus analyser did not start within {timeout} seconds",
                phase="start",
            )

        # wait for the autofocus analyser to stop running
        last_log_message = ""
        while datetime.datetime.now() < end:
            s = ps3_client.focus_status()
            status = PS3AutofocusStatus(**s if s else {})
            logger.info(f"{op}: {s=}")
            if not status.is_running:
                last_log_message = status.last_log_message
                break
            else:
                time.sleep(0.5)

        if datetime.datetime.now() >= end:
            raise FocusAnalysisError(
                f"autofocus analyser did not finish within {timeout} seconds",
                phase="finish",
            )

        assert status is not None
        status.last_log_message = last_log_message
        return status
    finally:
        ps3_client.close()
