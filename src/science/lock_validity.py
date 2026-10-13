"""Is the guider holding the star it locked onto, or something that is not a star?

This answers a different question from `sky_quality.SeeingQualityWhilePHD2Guiding`,
which scores *how steady the atmosphere is* while guiding proceeds. Both can be
excellent at once while the guide loop chases a noise fluctuation, and on
2026-09-08 they were: PHD2 reported successful guide steps for eleven minutes
against locks of a few thousand counts, and nothing in the stack disagreed.

**Why two tests rather than one.** They fail in opposite places.

The *session* test -- this frame's mass against the running median of the frames
before it -- is the one that works. Measured over that night it separates
completely: artifact locks sit below 0.021 of their session median, sound locks
above 0.085, and of 1100 frames not one falls between. It needs history, though,
so it says nothing about the first frames of a session, and nothing at all about a
session that *begins* on an artifact -- there the running median is the artifact
and the ratio reads 1.0.

The *stateless* test needs no history: a real star's peak stands above the local
sky, and its light fills the aperture. It covers the frames the session test
cannot, and it covers a second failure the session test also sees but that PHD2
usually catches first -- a hot pixel or cosmic ray, which is bright but sharp.

Neither is redundant. PHD2's own `MassChangeThreshold` is a third form of the same
idea over a 45 s window, and the window is emptied by the very collapse it exists
to catch: on 2026-09-08 it could judge 2 of 14 artifact frames.
"""

from __future__ import annotations

import datetime
import statistics
from collections import deque
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from common.config.phd2 import LockValidityConfig
from common.utils import isoformat_zulu

# ---------- models ----------


class LockValidity(StrEnum):
    Unknown = "Unknown"
    #: Too few frames to have a session brightness scale yet. The stateless test
    #: still runs, so an artifact is catchable here -- this is not "no opinion".
    WarmingUp = "WarmingUp"
    OnStar = "OnStar"
    #: PHD2 reported no usable star this frame. Recoverable and self-announcing;
    #: it costs time rather than data.
    NoLock = "NoLock"
    #: A lock was reported and it is not the star. This is the silent one.
    NotAStar = "NotAStar"


class LockMetrics(BaseModel):
    """One frame, as PHD2 reports it.

    `peak`, `background` and `background_sigma` arrive on `GuideStep` and
    `StarLost` from PHD2 2.6.14dev1mastbuild5 onward. Before that build they are
    absent, and `LockValidityConfig.require_background` decides whether that
    disables the stateless test or the whole component.
    """

    star_mass: float = Field(..., ge=0)
    snr: float = Field(default=0.0, ge=0)
    hfd_pixels: float = Field(default=0.0, ge=0)
    peak: float | None = None
    background: float | None = None
    background_sigma: float | None = None
    lost: bool = False
    guiding_paused: bool = False


class LockAssessment(BaseModel):
    """One episode of the guider not being on a star, kept after it ends.

    An operator asked to approve stopping the guide needs the evidence, and the
    live state is a poor place to find it: on 2026-09-08 the guider flickered
    between accepting an artifact and losing it altogether, so anyone reading
    `validity` at an arbitrary moment saw whichever it happened to be. This
    latches the episode -- when it began, how long it has run, and the numbers
    that made the call -- so the case survives the state recovering.
    """

    began_at: str
    frames: int = 0
    #: Worst (lowest) mass fraction seen in the episode, and the frame count over
    #: which it held. A single bad frame and eleven minutes of them look identical
    #: in `validity`; they should not look identical to a person deciding.
    worst_mass_fraction: float | None = None
    worst_peak_sigma_over_background: float | None = None
    reasons: list[str] = Field(default_factory=list)
    #: True while this is still happening. False once the guider recovered, and
    #: the record is then kept as history rather than as a live finding.
    ongoing: bool = True


class LockValidityState(BaseModel):
    validity: LockValidity = LockValidity.Unknown
    #: This frame's own verdict, before hysteresis. The two answer different
    #: questions and both have a consumer: a record must not average an artifact
    #: frame into a measurement even if the *session* had not yet been declared
    #: bad, while an alarm must not fire on one frame of bad seeing. Debouncing
    #: the frame verdict would lose an episode that lasts a single frame -- and
    #: one of 2026-09-08's two did.
    frame_verdict: LockValidity = LockValidity.Unknown
    #: The scale the session established, and this frame against it. Both None
    #: until `warmup_frames` frames have carried a mass.
    session_mass_scale: float | None = None
    mass_fraction: float | None = None
    #: The stateless pair, None when the build does not report background.
    peak_sigma_over_background: float | None = None
    mass_over_peak_hfd2: float | None = None
    #: Which test objected, for a log line that says why rather than just what.
    reasons: list[str] = Field(default_factory=list)
    frames_seen: int = 0
    #: The current or most recent episode of NotAStar, kept after it ends so the
    #: evidence is still there when a person comes to look. This is what a decision
    #: to stop the guide should be made against, not the instantaneous `validity`.
    worst_assessment: LockAssessment | None = None
    #: Set for exactly the update that moves *into* NotAStar. The alarm and the
    #: mid-cycle abort key on this, not on the state: 2026-09-08's lost-star beep
    #: fired 635 times because it keyed on the condition rather than its onset.
    entered_not_a_star: bool = False


class GuideLockSupervisor(BaseModel):
    """Session-scale and stateless validity checks over a stream of guide frames.

    Pure: `update()` is the only entry point, it does no I/O, and every decision it
    makes is reconstructible from the state it returns.
    """

    config: LockValidityConfig = LockValidityConfig()
    state: LockValidityState = LockValidityState()
    _scale_window: deque[float] = deque()
    _pending: LockValidity | None = None
    _pending_count: int = 0

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def reset(self) -> None:
        """Start a new guiding session.

        The scale belongs to one session and to one field. Carrying it across a
        re-guide is how a supervisor would judge a faint field against a bright
        field's brightness -- the exact error an absolute threshold makes.
        """
        self._scale_window = deque()
        self._pending = None
        self._pending_count = 0
        self.state = LockValidityState()

    def update(self, frame: LockMetrics, config: LockValidityConfig | None = None) -> LockValidityState:
        """Judge one frame.

        `config` is passed per frame rather than held, so a threshold edited in the
        controller DB takes effect on the next frame instead of at the next restart.
        The connector that owns this reads `unit_conf.phd2.lock_validity` live and
        hands it in; the default is only for tests and for a caller with no DB.
        """
        config = config or self.config
        state = self.state
        state.entered_not_a_star = False

        if frame.guiding_paused:
            return state

        state.frames_seen += 1
        reasons: list[str] = []

        if frame.lost or not frame.star_mass:
            state.frame_verdict = LockValidity.NoLock
            self._settle(LockValidity.NoLock, ["PHD2 reported no usable star"], config)
            return state

        stateless_bad = self._stateless_verdict(frame, reasons, config)
        session_bad = self._session_verdict(frame, reasons, config)

        if stateless_bad or session_bad:
            verdict = LockValidity.NotAStar
        elif len(self._scale_window) < config.warmup_frames:
            verdict = LockValidity.WarmingUp
        else:
            verdict = LockValidity.OnStar

        state.frame_verdict = verdict
        self._record_evidence(verdict, reasons)

        # Only a frame judged sound feeds the scale. A collapse must not be allowed
        # to drag the baseline it is measured against -- that is the failure that
        # defeats PHD2's own 45 s window, and a median only delays it.
        if verdict is not LockValidity.NotAStar:
            self._scale_window.append(frame.star_mass)
            while len(self._scale_window) > config.scale_window_frames:
                self._scale_window.popleft()

        self._settle(verdict, reasons, config)
        return state

    # ---------- the two tests ----------

    def _stateless_verdict(self, frame: LockMetrics, reasons: list[str], config: LockValidityConfig) -> bool:
        """Does this lock stand above the sky, and does its light fill the aperture?

        Needs nothing remembered, so it is the only thing with an opinion during
        warm-up and the only thing that can catch a session that begins on an
        artifact -- where the session scale is itself the artifact.
        """
        state = self.state
        state.peak_sigma_over_background = None
        state.mass_over_peak_hfd2 = None

        if frame.peak is None or frame.background is None:
            if config.require_background:
                reasons.append("build does not report peak/background")
            return False

        if frame.background_sigma:
            excess = (frame.peak - frame.background) / frame.background_sigma
            state.peak_sigma_over_background = excess
            if excess < config.min_peak_sigma_over_background:
                reasons.append(f"peak {excess:.1f}sigma over sky, want {config.min_peak_sigma_over_background:.0f}")

        if frame.peak > 0 and frame.hfd_pixels > 0:
            # Normalised by the seeing disc: raw mass/peak tracks HFD at r = 0.90,
            # so an un-normalised floor condemns sharp stars on a good night.
            ratio = frame.star_mass / (frame.peak * frame.hfd_pixels**2)
            state.mass_over_peak_hfd2 = ratio
            if ratio < config.min_mass_over_peak_hfd2:
                reasons.append(f"concentration {ratio:.2f}, want {config.min_mass_over_peak_hfd2:.2f}")

        # Both halves must object. Either alone is a real population of sound
        # frames: a bright star with a tight core sits low on mass/peak, and a
        # faint one in a bright sky sits low on peak-over-sky.
        return len(reasons) >= 2

    def _session_verdict(self, frame: LockMetrics, reasons: list[str], config: LockValidityConfig) -> bool:
        """This frame's mass against the brightness the session established."""
        state = self.state
        if len(self._scale_window) < config.warmup_frames:
            state.session_mass_scale = None
            state.mass_fraction = None
            return False

        scale = statistics.median(self._scale_window)
        state.session_mass_scale = scale
        fraction = frame.star_mass / scale if scale else None
        state.mass_fraction = fraction
        if fraction is not None and fraction < config.artifact_mass_fraction:
            reasons.append(f"mass {fraction:.3f} of session scale, want {config.artifact_mass_fraction:.2f}")
            return True
        return False

    def _record_evidence(self, verdict: LockValidity, reasons: list[str]) -> None:
        """Accumulate the case for the live episode, if there is one."""
        assessment = self.state.worst_assessment
        if assessment is None or not assessment.ongoing or verdict is not LockValidity.NotAStar:
            return
        assessment.frames += 1
        assessment.reasons = reasons
        fraction = self.state.mass_fraction
        if fraction is not None and (assessment.worst_mass_fraction is None or fraction < assessment.worst_mass_fraction):
            assessment.worst_mass_fraction = fraction
        excess = self.state.peak_sigma_over_background
        if excess is not None and (
            assessment.worst_peak_sigma_over_background is None or excess < assessment.worst_peak_sigma_over_background
        ):
            assessment.worst_peak_sigma_over_background = excess

    # ---------- state transitions ----------

    def _settle(self, verdict: LockValidity, reasons: list[str], config: LockValidityConfig) -> None:
        """Hold a verdict for `hysteresis_frames` before it becomes the state.

        One frame of bad seeing should not flip the state, and one lucky frame
        should not clear it. WarmingUp and Unknown are bookkeeping rather than
        findings, so they apply at once.
        """
        state = self.state

        if verdict in (LockValidity.WarmingUp, LockValidity.Unknown) or verdict == state.validity:
            self._pending = None
            self._pending_count = 0
            if verdict != state.validity:
                state.validity = verdict
            state.reasons = reasons
            return

        if verdict == self._pending:
            self._pending_count += 1
        else:
            self._pending = verdict
            self._pending_count = 1

        state.reasons = reasons
        if self._pending_count >= config.hysteresis_frames:
            was = state.validity
            state.validity = verdict
            state.entered_not_a_star = verdict is LockValidity.NotAStar and was is not LockValidity.NotAStar
            if state.entered_not_a_star:
                state.worst_assessment = LockAssessment(began_at=isoformat_zulu(datetime.datetime.now(datetime.UTC)))
            elif was is LockValidity.NotAStar and state.worst_assessment is not None:
                # Kept, not cleared: the operator may only look after it recovered.
                state.worst_assessment.ongoing = False
            self._pending = None
            self._pending_count = 0

    @property
    def frame_is_suspect(self) -> bool:
        """Did *this* frame measure something that is not the star?

        What a per-frame consumer keys on -- the harness refusing to write a
        verdict from a bracket whose measured frames are artifacts, and the
        reduction's `lock_quality`. Distinct from `validity`, which is debounced
        and is what an operator-facing alarm should use.
        """
        return self.state.frame_verdict is LockValidity.NotAStar

    @property
    def should_end_guiding(self) -> bool:
        """The action, kept separate from the finding.

        Default off. The supervisor is meant to report for a night first, so the
        state can be read against what actually happened before it is allowed to
        act -- and so the action can be withdrawn without losing the signal.
        """
        return self.config.end_guiding_on_not_a_star and self.state.validity is LockValidity.NotAStar
