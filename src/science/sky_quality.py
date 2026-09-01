from __future__ import annotations

import datetime
import math
import statistics
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from common.utils import isoformat_zulu

# ---------- helpers ----------


def median_absolute_deviation(values: list[float]) -> float:
    """Robust spread estimate (MAD) scaled to ~std for normal data."""
    if not values:
        return 0.0
    median_value = statistics.median(values)
    deviations = [abs(x - median_value) for x in values]
    return 1.4826 * statistics.median(deviations)


# ---------- pydantic models ----------


class QualityState(StrEnum):
    Unknown = "Unknown"
    WarmingUp = "WarmingUp"
    Excellent = "Excellent"
    Good = "Good"
    Fair = "Fair"
    Poor = "Poor"


class FrameMetrics(BaseModel):
    snr: float = Field(..., gt=0)
    hfd_pixels: float = Field(..., gt=0)
    saturated: bool = False
    guiding_paused: bool = False


class SeeingQualityWhilePHD2GuidingConfig(BaseModel):
    window_seconds: int = Field(default=180, ge=30)  # rolling history ~3 minutes
    frames_per_second: float = Field(default=1.0, gt=0)
    weight_snr: float = 1.0
    weight_hfd: float = 1.0
    ewma_alpha: float = Field(default=0.25, gt=0, lt=1)
    threshold_excellent: float = 80.0
    threshold_good: float = 60.0
    threshold_fair: float = 40.0
    warmup_min_samples: int = Field(default=20, ge=5)
    hysteresis_margin: float = Field(default=3.0, ge=0)
    plate_scale_arcsec_per_pixel: float | None = Field(
        default=0.262578, description="Arcseconds per guide-camera pixel (e.g., 0.253)."
    )

    @property
    def max_history_length(self) -> int:
        return max(10, int(self.window_seconds * self.frames_per_second))


class SeeingQualityWhilePHD2GuidingState(BaseModel):
    # Relative score/state
    score_0_to_100: float = 0.0
    quality_state: QualityState = QualityState.Unknown
    ewma_zscore: float | None = None

    # Rolling histories (pixels)
    snr_history: list[float] = Field(default_factory=list)
    hfd_history_pixels: list[float] = Field(default_factory=list)

    # Absolute seeing (arcsec), computed only if plate_scale is set
    last_hfd_arcsec: float | None = None
    median_hfd_arcsec: float | None = None

    @field_validator("snr_history", "hfd_history_pixels")
    @classmethod
    def _force_floats(cls, values: list[float]) -> list[float]:
        return [float(x) for x in values]


class SeeingQualityWhilePHD2Guiding(BaseModel):
    config: SeeingQualityWhilePHD2GuidingConfig = SeeingQualityWhilePHD2GuidingConfig()
    state: SeeingQualityWhilePHD2GuidingState = SeeingQualityWhilePHD2GuidingState()
    latest_update: str | None = None

    def update(self, frame: FrameMetrics) -> SeeingQualityWhilePHD2GuidingState:
        """Update the seeing quality estimate from one PHD2 frame."""
        config = self.config
        state = self.state

        # Skip frames that shouldn't affect stats
        if frame.guiding_paused or frame.saturated:
            return state

        # Append with rolling cap
        state.snr_history.append(frame.snr)
        state.hfd_history_pixels.append(frame.hfd_pixels)
        self._trim_histories()

        # Optionally compute absolute HFD in arcsec for this frame
        if config.plate_scale_arcsec_per_pixel is not None:
            state.last_hfd_arcsec = frame.hfd_pixels * config.plate_scale_arcsec_per_pixel
            # Rolling median of absolute HFD (arcsec)
            state.median_hfd_arcsec = statistics.median(
                [h * config.plate_scale_arcsec_per_pixel for h in state.hfd_history_pixels]
            )

        # Warm-up
        if len(state.snr_history) < config.warmup_min_samples:
            state.score_0_to_100 = 0.0
            state.quality_state = QualityState.WarmingUp
            return state

        # Robust z-scores
        median_snr = statistics.median(state.snr_history)
        mad_snr = median_absolute_deviation(state.snr_history) or 1e-6
        snr_zscore = (frame.snr - median_snr) / mad_snr

        median_hfd_pixels = statistics.median(state.hfd_history_pixels)
        mad_hfd_pixels = median_absolute_deviation(state.hfd_history_pixels) or 1e-6
        hfd_zscore = (frame.hfd_pixels - median_hfd_pixels) / mad_hfd_pixels  # higher is worse

        combined_zscore = config.weight_snr * snr_zscore - config.weight_hfd * hfd_zscore

        # EWMA smoothing
        if state.ewma_zscore is None:
            state.ewma_zscore = combined_zscore
        else:
            alpha = config.ewma_alpha
            state.ewma_zscore = alpha * combined_zscore + (1 - alpha) * state.ewma_zscore

        # Logistic mapping to 0..100
        steepness = 0.9
        state.score_0_to_100 = 100.0 / (1.0 + math.exp(-steepness * state.ewma_zscore))

        # Hysteretic state mapping
        state.quality_state = self._compute_next_state(previous_state=state.quality_state, score=state.score_0_to_100)

        self.latest_update = isoformat_zulu(datetime.datetime.now(datetime.UTC))

        return state

    # ---------- internals ----------
    def _trim_histories(self) -> None:
        maxlen = self.config.max_history_length
        if len(self.state.snr_history) > maxlen:
            del self.state.snr_history[:-maxlen]
        if len(self.state.hfd_history_pixels) > maxlen:
            del self.state.hfd_history_pixels[:-maxlen]

    def _compute_next_state(  # noqa: C901
        self, previous_state: QualityState, score: float
    ) -> QualityState:
        config = self.config
        margin = config.hysteresis_margin

        if previous_state in (QualityState.Unknown, QualityState.WarmingUp):
            return self._map_score_to_state(score)

        if previous_state == QualityState.Excellent:
            if score >= config.threshold_excellent - margin:
                return QualityState.Excellent
            elif score >= config.threshold_good:
                return QualityState.Good
            elif score >= config.threshold_fair:
                return QualityState.Fair
            else:
                return QualityState.Poor

        if previous_state == QualityState.Good:
            if score >= config.threshold_excellent + margin:
                return QualityState.Excellent
            elif score < config.threshold_good - margin:
                return QualityState.Fair if score >= config.threshold_fair else QualityState.Poor
            else:
                return QualityState.Good

        if previous_state == QualityState.Fair:
            if score >= config.threshold_good + margin:
                return QualityState.Good
            elif score < config.threshold_fair - margin:
                return QualityState.Poor
            else:
                return QualityState.Fair

        if previous_state == QualityState.Poor:
            if score < config.threshold_fair + margin:
                return QualityState.Poor
            elif score < config.threshold_good + margin:
                return QualityState.Fair
            else:
                return QualityState.Good

        return self._map_score_to_state(score)

    def _map_score_to_state(self, score: float) -> QualityState:
        config = self.config
        if score >= config.threshold_excellent:
            return QualityState.Excellent
        if score >= config.threshold_good:
            return QualityState.Good
        if score >= config.threshold_fair:
            return QualityState.Fair
        return QualityState.Poor
