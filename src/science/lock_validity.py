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

import statistics
from collections import deque
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

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


class LockValidityConfig(BaseModel):
    #: Frames before the session scale is trusted. Eight is ~113 s at the measured
    #: 9.58 s cadence. Below it the 2026-09-08 replay produces a false alarm; above
    #: it nothing improves, and the closest sound frame sits 1.34x clear of the cut.
    warmup_frames: int = Field(default=8, ge=3, le=200)

    #: How many masses the scale is taken over. Sixty is ~10 minutes -- long enough
    #: to be stable, short enough to follow a field change after a re-guide.
    scale_window_frames: int = Field(default=60, ge=10, le=1000)

    #: Below this fraction of the session scale the lock is not that object. The
    #: empty band on 2026-09-08 runs 0.021 to 0.085, so 0.05 sits in the middle of
    #: a region containing no frames at all.
    artifact_mass_fraction: float = Field(default=0.05, gt=0.0, lt=1.0)

    #: Stateless test. A real star's peak stands clear of the sky; an artifact's
    #: peak *is* the sky. Expressed in sigma so it is free of the exposure, the
    #: gain and the moon -- an absolute ADU threshold is none of those things.
    min_peak_sigma_over_background: float = Field(default=15.0, gt=0)

    #: Stateless test, second half: flux per unit peak. A star fills the aperture
    #: and gives ~40; a few noise pixels over threshold give ~5. Set between them,
    #: nearer the artifacts, because a bright star with a tight core lands low.
    min_mass_over_peak: float = Field(default=12.0, gt=0)

    #: Frames a verdict must persist before the state changes. One frame of bad
    #: seeing should not flip the state, and one good frame should not clear it.
    hysteresis_frames: int = Field(default=2, ge=1, le=20)

    #: Without the PHD2 build that reports peak and background, run the session
    #: test alone rather than refusing to run. False is the honest default: half a
    #: check is better than none, and the log says which half is missing.
    require_background: bool = False

    #: **Does reaching NotAStar stop the guiding?** Detection and action are
    #: separate switches on purpose. The supervisor is meant to run for a night
    #: reporting only, so the state can be read against what actually happened
    #: before anything acts on it -- and so the action can be withdrawn without
    #: losing the signal if it proves too eager.
    end_guiding_on_not_a_star: bool = False


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
    mass_over_peak: float | None = None
    #: Which test objected, for a log line that says why rather than just what.
    reasons: list[str] = Field(default_factory=list)
    frames_seen: int = 0
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

    def update(self, frame: LockMetrics) -> LockValidityState:
        state = self.state
        config = self.config
        state.entered_not_a_star = False

        if frame.guiding_paused:
            return state

        state.frames_seen += 1
        reasons: list[str] = []

        if frame.lost or not frame.star_mass:
            state.frame_verdict = LockValidity.NoLock
            self._settle(LockValidity.NoLock, ["PHD2 reported no usable star"])
            return state

        stateless_bad = self._stateless_verdict(frame, reasons)
        session_bad = self._session_verdict(frame, reasons)

        if stateless_bad or session_bad:
            verdict = LockValidity.NotAStar
        elif len(self._scale_window) < config.warmup_frames:
            verdict = LockValidity.WarmingUp
        else:
            verdict = LockValidity.OnStar

        state.frame_verdict = verdict

        # Only a frame judged sound feeds the scale. A collapse must not be allowed
        # to drag the baseline it is measured against -- that is the failure that
        # defeats PHD2's own 45 s window, and a median only delays it.
        if verdict is not LockValidity.NotAStar:
            self._scale_window.append(frame.star_mass)
            while len(self._scale_window) > config.scale_window_frames:
                self._scale_window.popleft()

        self._settle(verdict, reasons)
        return state

    # ---------- the two tests ----------

    def _stateless_verdict(self, frame: LockMetrics, reasons: list[str]) -> bool:
        """Does this lock stand above the sky, and does its light fill the aperture?

        Needs nothing remembered, so it is the only thing with an opinion during
        warm-up and the only thing that can catch a session that begins on an
        artifact -- where the session scale is itself the artifact.
        """
        state = self.state
        config = self.config
        state.peak_sigma_over_background = None
        state.mass_over_peak = None

        if frame.peak is None or frame.background is None:
            if config.require_background:
                reasons.append("build does not report peak/background")
            return False

        if frame.background_sigma:
            excess = (frame.peak - frame.background) / frame.background_sigma
            state.peak_sigma_over_background = excess
            if excess < config.min_peak_sigma_over_background:
                reasons.append(f"peak {excess:.1f}sigma over sky, want {config.min_peak_sigma_over_background:.0f}")

        if frame.peak > 0:
            ratio = frame.star_mass / frame.peak
            state.mass_over_peak = ratio
            if ratio < config.min_mass_over_peak:
                reasons.append(f"mass/peak {ratio:.1f}, want {config.min_mass_over_peak:.0f}")

        # Both halves must object. Either alone is a real population of sound
        # frames: a bright star with a tight core sits low on mass/peak, and a
        # faint one in a bright sky sits low on peak-over-sky.
        return len(reasons) >= 2

    def _session_verdict(self, frame: LockMetrics, reasons: list[str]) -> bool:
        """This frame's mass against the brightness the session established."""
        state = self.state
        config = self.config
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

    # ---------- state transitions ----------

    def _settle(self, verdict: LockValidity, reasons: list[str]) -> None:
        """Hold a verdict for `hysteresis_frames` before it becomes the state.

        One frame of bad seeing should not flip the state, and one lucky frame
        should not clear it. WarmingUp and Unknown are bookkeeping rather than
        findings, so they apply at once.
        """
        state = self.state
        config = self.config

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
