"""The guide-lock supervisor, against the shapes 2026-09-08 actually produced.

Numbers here are that night's: a session guiding at ~500,000 counts on a sky of
~980 ADU, and artifact locks at ~5,300 counts whose peak sits *at* the sky.
"""

from __future__ import annotations

import pytest

from science.lock_validity import GuideLockSupervisor, LockMetrics, LockValidity

STAR = dict(star_mass=500_000.0, snr=41.0, hfd_pixels=6.7, peak=12_000.0, background=980.0, background_sigma=40.0)
#: A noise fluctuation: star-shaped HFD, plausible SNR, and a peak that is the sky.
ARTIFACT = dict(star_mass=5_300.0, snr=14.9, hfd_pixels=6.3, peak=983.0, background=980.0, background_sigma=40.0)


def feed(supervisor: GuideLockSupervisor, sample: dict, count: int):
    state = supervisor.state
    for _ in range(count):
        state = supervisor.update(LockMetrics(**sample))
    return state


def test_warms_up_before_it_has_a_scale():
    s = GuideLockSupervisor()
    state = feed(s, STAR, s.config.warmup_frames - 1)
    assert state.validity is LockValidity.WarmingUp
    assert state.session_mass_scale is None


def test_reaches_on_star_after_warmup():
    s = GuideLockSupervisor()
    state = feed(s, STAR, s.config.warmup_frames + 2)
    assert state.validity is LockValidity.OnStar
    assert state.session_mass_scale == pytest.approx(500_000.0)


def test_artifact_during_warmup_is_caught_with_no_history():
    """The stateless half is the only thing with an opinion here, and it must have one.

    A session that *begins* on an artifact is the case the session scale cannot
    ever catch -- the running median would be the artifact itself.
    """
    s = GuideLockSupervisor()
    state = feed(s, ARTIFACT, 3)
    assert state.frame_verdict is LockValidity.NotAStar
    assert state.session_mass_scale is None


def test_single_frame_artifact_is_reported_per_frame():
    """One of 2026-09-08's two episodes was a single frame, so debouncing detection
    would have lost it entirely. The frame verdict is not debounced; the state is."""
    s = GuideLockSupervisor()
    feed(s, STAR, 20)
    state = s.update(LockMetrics(**ARTIFACT))
    assert state.frame_verdict is LockValidity.NotAStar
    assert s.frame_is_suspect
    assert state.validity is LockValidity.OnStar  # not yet, and correctly so


def test_sustained_artifact_moves_the_state_and_reports_the_onset_once():
    s = GuideLockSupervisor()
    feed(s, STAR, 20)
    onsets = 0
    for _ in range(6):
        if s.update(LockMetrics(**ARTIFACT)).entered_not_a_star:
            onsets += 1
    assert s.state.validity is LockValidity.NotAStar
    assert onsets == 1, "an alarm keyed on the onset must fire once, not once per frame"


def test_artifacts_do_not_drag_the_scale_they_are_measured_against():
    """The failure that defeats PHD2's own 45 s window: a long episode walking the
    baseline down until each successive artifact looks ordinary."""
    s = GuideLockSupervisor()
    feed(s, STAR, 20)
    scale_before = s.state.session_mass_scale
    feed(s, ARTIFACT, 30)
    assert s.state.session_mass_scale == pytest.approx(scale_before)


def test_a_faint_field_is_not_condemned_for_being_faint():
    """Pisces guided at 70,195 counts where an earlier field ran at 2,000,000. Any
    absolute threshold either condemns one or misses the artifact in the other."""
    s = GuideLockSupervisor()
    faint = dict(STAR, star_mass=70_195.0, peak=4_000.0, snr=17.5)
    state = feed(s, faint, 20)
    assert state.validity is LockValidity.OnStar


def test_lost_star_is_not_an_artifact():
    s = GuideLockSupervisor()
    feed(s, STAR, 20)
    state = s.update(LockMetrics(star_mass=0.0, lost=True))
    assert state.frame_verdict is LockValidity.NoLock


def test_paused_frames_are_ignored_entirely():
    """The bracketed handover pauses the guide loop with the lock held."""
    s = GuideLockSupervisor()
    feed(s, STAR, 20)
    seen = s.state.frames_seen
    s.update(LockMetrics(**dict(ARTIFACT, guiding_paused=True)))
    assert s.state.frames_seen == seen
    assert s.state.validity is LockValidity.OnStar


def test_the_action_is_off_by_default_and_separable_from_the_finding():
    s = GuideLockSupervisor()
    feed(s, STAR, 20)
    feed(s, ARTIFACT, 4)
    assert s.state.validity is LockValidity.NotAStar
    assert not s.should_end_guiding
    s.config.end_guiding_on_not_a_star = True
    assert s.should_end_guiding


def test_runs_without_background_on_an_older_phd2_build():
    """Peak and background arrive only from 2.6.14dev1mastbuild5 onward."""
    s = GuideLockSupervisor()
    bare = {k: v for k, v in STAR.items() if k not in ("peak", "background", "background_sigma")}
    state = feed(s, bare, 20)
    assert state.validity is LockValidity.OnStar
    assert state.peak_sigma_over_background is None
    state = s.update(LockMetrics(star_mass=5_300.0))
    assert state.frame_verdict is LockValidity.NotAStar, "the session test still works alone"


def test_thresholds_are_read_live_per_frame():
    """A threshold edited in the controller DB must bite on the next frame.

    Snapshotting configuration is how a mid-session `phd2.settle` edit became a
    silent no-op on 2026-09-02, and the whole point of putting these numbers in
    the DB is that a night can retune them without a deployment.
    """
    from common.config.phd2 import LockValidityConfig

    s = GuideLockSupervisor()
    feed(s, STAR, 20)
    faint = dict(STAR, star_mass=100_000.0)  # 0.2 of the scale: sound by default
    assert s.update(LockMetrics(**faint)).frame_verdict is LockValidity.OnStar

    strict = LockValidityConfig(artifact_mass_fraction=0.5)
    assert s.update(LockMetrics(**faint), config=strict).frame_verdict is LockValidity.NotAStar


def test_the_assessment_survives_the_guider_recovering():
    """An operator asked to approve stopping the guide may only look afterwards.

    On 2026-09-08 the guider flickered between accepting an artifact and losing
    it, so a person reading `validity` at an arbitrary moment saw whichever it
    happened to be. The episode has to outlive the state.
    """
    s = GuideLockSupervisor()
    feed(s, STAR, 20)
    feed(s, ARTIFACT, 6)
    assert s.state.validity is LockValidity.NotAStar
    assessment = s.state.worst_assessment
    assert assessment is not None and assessment.ongoing
    assert assessment.frames >= 4
    assert assessment.worst_mass_fraction < 0.05

    feed(s, STAR, 6)
    assert s.state.validity is LockValidity.OnStar
    kept = s.state.worst_assessment
    assert kept is not None, "the case must still be there once the guider recovers"
    assert not kept.ongoing
    assert kept.frames >= 4
    assert kept.worst_mass_fraction < 0.05


def test_one_bad_frame_and_a_long_episode_are_distinguishable():
    """Both read NotAStar. Only the frame count separates them, and that is the
    difference between a glitch and eleven minutes of guiding on nothing."""
    brief = GuideLockSupervisor()
    feed(brief, STAR, 20)
    feed(brief, ARTIFACT, 3)
    long = GuideLockSupervisor()
    feed(long, STAR, 20)
    feed(long, ARTIFACT, 40)
    assert brief.state.validity is long.state.validity is LockValidity.NotAStar
    assert long.state.worst_assessment.frames > 10 * brief.state.worst_assessment.frames
