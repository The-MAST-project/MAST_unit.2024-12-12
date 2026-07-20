"""The structural contract shared by the focus analyzers.

Two analyzers produce a focus solution -- the external ps3cli one
(``focus_analysis.PS3*``, PlaneWave's, still driving operational autofocus) and
the self-contained HFD one (:mod:`calibration.analysis.models`, driving
``/calibrate/focuser``).  Consumers that only read a solution (the V-curve
plotter, report generators) should accept *either*, without importing either.

These protocols express that contract structurally: neither model class inherits
from or knows about them, and a type checker verifies conformance at each call
site -- unlike ``typing.cast()``, which merely silences the checker and lets the
two shapes drift apart undetected.

Two deliberate details, both of which break the naive version:

* **Members are read-only properties, not plain attributes.**  A pydantic field
  satisfies a ``@property`` member, but a ``@property`` does *not* satisfy a
  plain (mutable) attribute member.  ``PS3FocusSample.star_diameter_pixels`` is
  a property aliasing the ps3cli wire name, so the property form is what lets
  both classes conform.
* **``focus_samples`` is a ``Sequence``, not a ``list``.**  ``list`` is
  invariant, so ``list[PS3FocusSample]`` would not satisfy
  ``list[FocusSampleLike]``; ``Sequence`` is covariant and does.

The neutral vocabulary is intentional.  ``star_diameter_pixels`` says what every
analyzer actually reports -- a per-frame star size in pixels -- where the ps3cli
field name (``star_rms_diameter_pixels``) describes an RMS diameter that the HFD
analyzer does not measure.  That name stays confined to ``focus_analysis.py``,
where it is accurate: it is PlaneWave's JSON wire key.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class FocusSampleLike(Protocol):
    """One focuser position and the star size measured there."""

    @property
    def is_valid(self) -> bool: ...

    @property
    def focus_position(self) -> float | None: ...

    @property
    def num_stars(self) -> int | None: ...

    @property
    def star_diameter_pixels(self) -> float | None:
        """Measured star size at this position (px).

        HFD (half-flux diameter) for the self-contained analyzer, ps3cli's star
        RMS diameter for the external one -- different metrics, same role: the
        ordinate of the V-curve.  Comparable *within* one analyzer's sweep, not
        across analyzers.
        """
        ...


@runtime_checkable
class FocusAnalysisResultLike(Protocol):
    """A fitted V-curve solution: ``D^2 = a*x^2 + b*x + c``.

    The ``vcurve_*`` names are kept from the ps3cli model because they are
    honest for both -- the HFD analyzer fits the same quadratic-in-D^2 form.
    """

    @property
    def has_solution(self) -> bool: ...

    @property
    def best_focus_position(self) -> float | None: ...

    @property
    def best_focus_star_diameter(self) -> float | None: ...

    @property
    def tolerance(self) -> float | None: ...

    @property
    def vcurve_a(self) -> float | None: ...

    @property
    def vcurve_b(self) -> float | None: ...

    @property
    def vcurve_c(self) -> float | None: ...

    @property
    def focus_samples(self) -> Sequence[FocusSampleLike] | None: ...

    @property
    def errors(self) -> list[str] | None: ...
