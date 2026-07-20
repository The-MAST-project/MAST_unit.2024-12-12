"""Result models for the self-contained HFD focus analyzer.

The HFD counterparts to ``focus_analysis.PS3FocusSample`` /
``PS3FocusAnalysisResult`` / ``PS3AutofocusStatus``.  They satisfy
:mod:`calibration.analysis.protocols` structurally, so a consumer typed against
the protocol takes either analyzer's output with no cast and no inheritance.

They are *not* field-for-field copies of the ps3cli models.  Three of those
fields describe ps3cli rather than focus, and carrying them here would put dead
or misleading values into the replay bundles the on-sky harness mines:

* ``star_rms_diameter_pixels`` -> ``hfd_pixels``.  HFD is a **half-flux**
  diameter, not an RMS diameter.  The neutral protocol name
  ``star_diameter_pixels`` is exposed as a property over it.
* ``is_running`` -- dropped.  ps3cli is an external server that gets polled;
  this analyzer is synchronous, so the field could only ever be ``False``.
* ``last_log_message`` -> ``message``.  Same content (a one-line summary of the
  fit), named for what it is rather than for the polling loop it came from.

``ExtendedBaseModel`` (not bare ``BaseModel``) because HFD legitimately produces
NaN -- a frame with no usable star yields ``nan`` -- and it is the base that
round-trips NaN/Infinity through JSON instead of emitting invalid JSON.
"""

from __future__ import annotations

from common.extended_basemodel import ExtendedBaseModel


class HFDFocusSample(ExtendedBaseModel):
    """One frame of a sweep: where the focuser was, and the HFD measured there."""

    is_valid: bool
    focus_position: float | None = None
    num_stars: int | None = None
    hfd_pixels: float | None = None

    @property
    def star_diameter_pixels(self) -> float | None:
        """Neutral alias for :attr:`hfd_pixels` (satisfies ``FocusSampleLike``)."""
        return self.hfd_pixels


class HFDAutofocusResult(ExtendedBaseModel):
    """The fitted V-curve: ``D^2 = a*x^2 + b*x + c``, vertex at ``-b/2a``.

    ``n_consistent_stars`` has no ps3cli counterpart: the HFD sweep is measured
    jointly over a star set cross-matched across all frames
    (:func:`calibration.analysis.hfd.measure_sweep_hfd`), and how many stars
    survived that matching is the primary confidence figure behind the fit.
    """

    has_solution: bool
    best_focus_position: float | None = None
    best_focus_star_diameter: float | None = None
    tolerance: float | None = None
    vcurve_a: float | None = None
    vcurve_b: float | None = None
    vcurve_c: float | None = None
    n_consistent_stars: int = 0
    focus_samples: list[HFDFocusSample] | None = []
    errors: list[str] | None = []


class HFDAutofocusStatus(ExtendedBaseModel):
    """Outcome of one HFD analysis run.

    ``message`` is a human-readable one-liner for logs and the run bundle's
    ``status.json``; ``analysis_result`` is ``None`` only if analysis could not
    be attempted at all (a failed *fit* still returns a result with
    ``has_solution=False`` and populated ``errors``).
    """

    message: str | None = None
    errors: list[str] | None = None
    analysis_result: HFDAutofocusResult | None = None
