"""The sky's quality while guiding, from PHD2's guide frames.

Each `GuideStep` reports the guide star's HFD, SNR and StarMass. Over one guiding session
(`StartGuiding` to `GuidingStopped`) they become `LatestGuidingSkyQuality`: medians over the
last 1, 3 and 5 minutes, seeing from the HFD (in arcseconds, through the plate scale) and
transparency from the StarMass relative to the session's first minute.

What it replaces scored each frame by its z-score against the last three minutes, so steady
bad seeing and steady good seeing both scored ~50: a trend, which could gate nothing. It was
also never reset between sessions, assumed a fixed plate scale, and read `SNR`/`HFD`
unguarded on PHD2's reader thread.
"""

from __future__ import annotations

import datetime
import math
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from common.mast_logging import get_logger
from common.models.latest import LatestGuidingSkyQuality, SkyQualityWindow
from common.utils import boxed_info

logger = get_logger(__name__)

#: The windows reported, shortest first.
WINDOWS_SECONDS = (60, 180, 300)

#: How long, from the session's first measured frame, its StarMass baseline is gathered over.
BASELINE_SECONDS = 60

#: The fewest frames a baseline is taken from: a minute of 30-second guide exposures is not one.
MIN_BASELINE_SAMPLES = 5

#: How often a session's summary is logged: on the first frame (or lost star) this long after
#: the last one, and once more when the session ends.
SUMMARY_SECONDS = 60


def median_absolute_deviation(values: list[float]) -> float:
    """Robust spread (MAD), scaled to a standard deviation for normal data."""
    if not values:
        return 0.0
    median_value = statistics.median(values)
    return 1.4826 * statistics.median([abs(x - median_value) for x in values])


def positive(ev: dict, key: str) -> float | None:
    """``ev[key]`` as a float, or None when it is missing, not a number, or not above zero."""
    value = ev.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


@dataclass(frozen=True)
class _Sample:
    at: float  # the clock's seconds
    hfd_px: float
    snr: float | None
    star_mass: float | None


class GuidingSkyQuality:
    """One guiding session's sky quality. Fed from PHD2's reader thread, read from any.

    The record is rebuilt on every frame, so a status read is a copy, not a computation.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._record: LatestGuidingSkyQuality | None = None
        self._samples: deque[_Sample] = deque()
        self._lost: deque[float] = deque()
        self._rejected: deque[float] = deque()
        self._first_sample_at: float | None = None
        self._started_at: float = 0.0
        self._summarised_at: float = 0.0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._record is not None and self._record.running

    @property
    def latest(self) -> LatestGuidingSkyQuality | None:
        """A copy of the current or last session's record; None before the first session."""
        with self._lock:
            return None if self._record is None else self._record.model_copy(deep=True)

    def start(self, plate_scale_arcsec: float | None, binning: int | None, plan_id: str | None = None) -> None:
        """A new session: everything from the last one is dropped."""
        with self._lock:
            self._samples.clear()
            self._lost.clear()
            self._rejected.clear()
            self._first_sample_at = None
            self._started_at = self._summarised_at = self._clock()
            self._record = LatestGuidingSkyQuality(
                plate_scale_arcsec=plate_scale_arcsec,
                binning=binning,
                baseline_seconds=BASELINE_SECONDS,
            ).begin(plan_id=plan_id)
            self._refresh()

    def stop(self, error: str | None = None) -> None:
        """End the session. Idempotent; a session that never started is left as it is."""
        with self._lock:
            if self._record is None or not self._record.running:
                return
            self._refresh()
            self._record.finish(succeeded=error is None, errors=[error] if error else None)
            lines = self._summary(self._clock(), ended=True)
        boxed_info(logger, lines)

    def guide_step(self, ev: dict) -> None:
        """One `GuideStep`. A frame PHD2 flagged, or without a usable HFD, counts as rejected."""
        hfd = positive(ev, "HFD")
        with self._lock:
            if self._record is None or not self._record.running:
                return
            now = self._clock()
            if hfd is None or "ErrorCode" in ev:
                self._rejected.append(now)
                self._record.rejected += 1
            else:
                self._samples.append(_Sample(now, hfd, positive(ev, "SNR"), positive(ev, "StarMass")))
                self._record.samples += 1
                self._record.last_sample = datetime.datetime.now(datetime.UTC).isoformat()
                if self._first_sample_at is None:
                    self._first_sample_at = now
            self._refresh(now)
            lines = self._summary_if_due(now)
        if lines:
            boxed_info(logger, lines)

    def star_lost(self) -> None:
        with self._lock:
            if self._record is None or not self._record.running:
                return
            now = self._clock()
            self._lost.append(now)
            self._record.star_lost += 1
            self._refresh(now)
            lines = self._summary_if_due(now)
        if lines:
            boxed_info(logger, lines)

    # ---------------------------------------------------------------- internals
    def _summary_if_due(self, now: float) -> list[str] | None:
        """The summary's lines once `SUMMARY_SECONDS` have passed since the last. Under the
        lock; the caller logs them after releasing it."""
        if now - self._summarised_at < SUMMARY_SECONDS:
            return None
        self._summarised_at = now
        return self._summary(now)

    def _summary(self, now: float, ended: bool = False) -> list[str]:
        """The session so far, one line per window. ASCII only: the daily log's readers."""
        record = self._record
        assert record is not None
        elapsed = int(now - self._started_at)
        scale = f'{record.plate_scale_arcsec:.3f}"/px' if record.plate_scale_arcsec else "no plate scale"
        title = "sky quality while guiding" + (" (session ended)" if ended else "")
        lines = [
            f"{title}{f', plan {record.plan_id}' if record.plan_id else ''}",
            f"{elapsed // 60}m{elapsed % 60:02d}s, {record.samples} frames, {record.star_lost} star lost, "
            f"{record.rejected} rejected, binning {record.binning}, {scale}",
        ]
        if record.errors:
            lines.append(f"errors: {'; '.join(record.errors)}")
        for w in record.windows:
            lines.append(f"{w.seconds // 60}m: {_window_text(w)}")
        return lines

    def _refresh(self, now: float | None = None) -> None:
        """Trim the histories and recompute the record's baseline and windows. Under the lock."""
        assert self._record is not None
        now = self._clock() if now is None else now
        horizon = now - max(WINDOWS_SECONDS)
        for history in (self._lost, self._rejected):
            while history and history[0] < horizon:
                history.popleft()
        if self._record.baseline_star_mass is not None:
            # The baseline's frames are needed only until it is taken.
            while self._samples and self._samples[0].at < horizon:
                self._samples.popleft()

        if (
            self._record.baseline_star_mass is None
            and self._first_sample_at is not None
            and now - self._first_sample_at >= BASELINE_SECONDS
        ):
            first = [
                s.star_mass
                for s in self._samples
                if s.star_mass is not None and s.at - self._first_sample_at <= BASELINE_SECONDS
            ]
            if len(first) >= MIN_BASELINE_SAMPLES:
                self._record.baseline_star_mass = statistics.median(first)

        self._record.windows = [self._window(seconds, now) for seconds in WINDOWS_SECONDS]

    def _window(self, seconds: int, now: float) -> SkyQualityWindow:
        assert self._record is not None
        cutoff = now - seconds
        samples = [s for s in self._samples if s.at >= cutoff]
        window = SkyQualityWindow(
            seconds=seconds,
            samples=len(samples),
            star_lost=sum(1 for t in self._lost if t >= cutoff),
            rejected=sum(1 for t in self._rejected if t >= cutoff),
        )
        if not samples:
            return window

        window.span_seconds = samples[-1].at - samples[0].at
        hfds = [s.hfd_px for s in samples]
        window.hfd_px = statistics.median(hfds)
        scale = self._record.plate_scale_arcsec
        if scale is not None:
            window.hfd_arcsec = window.hfd_px * scale
            window.hfd_arcsec_spread = median_absolute_deviation(hfds) * scale
        snrs = [s.snr for s in samples if s.snr is not None]
        window.snr = statistics.median(snrs) if snrs else None
        masses = [s.star_mass for s in samples if s.star_mass is not None]
        window.star_mass = statistics.median(masses) if masses else None
        baseline = self._record.baseline_star_mass
        if window.star_mass is not None and baseline:
            window.transparency = window.star_mass / baseline
        return window


def _window_text(w: SkyQualityWindow) -> str:
    if not w.samples:
        return f"no frames, {w.star_lost} lost, {w.rejected} rejected"
    if w.hfd_arcsec is not None:
        seeing = f'HFD {w.hfd_arcsec:.2f}" +/- {w.hfd_arcsec_spread or 0:.2f} ({w.hfd_px:.2f} px)'
    else:
        seeing = f"HFD {w.hfd_px:.2f} px"
    snr = f"SNR {w.snr:.0f}" if w.snr is not None else "SNR -"
    transparency = f"transparency {w.transparency:.2f}" if w.transparency is not None else "transparency -"
    return (
        f"{seeing}, {snr}, {transparency}, {w.samples} frames over {w.span_seconds:.0f}s, "
        f"{w.star_lost} lost, {w.rejected} rejected"
    )
