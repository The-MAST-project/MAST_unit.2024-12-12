"""Flux metering -- locating the fibre on the imager detector.

`acquire_and_find_max_flux` acquires a star, walks a small spiral, and finds the pointing
at which most light reaches the spectrograph fibre. Cross-correlating the imager frame taken
there against the one taken at the start gives the offset between where acquisition PUT the
star and where the fibre actually is:

    fiber_true = fiber_assumed + (dx, dy)

That offset, in detector pixels, is the whole deliverable. See
`mast-claude-config/plans/flux_metering_design.md`.

Nothing here writes configuration. `dx, dy` land in the run's JSON and are applied by hand,
if at all, once they have been shown to be consistent.
"""

from flux_metering.flux_meter import FluxMeter, FluxMeterError, SimulatedFluxMeter

__all__ = ["FluxMeter", "FluxMeterError", "SimulatedFluxMeter"]
