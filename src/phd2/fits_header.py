"""Record the sensor temperature an exposure was taken at, into its FITS header.

PHD2 writes the frame and its header records **no temperature at all** -- not
`CCD-TEMP`, not the setpoint, not cooler power -- even though the ASI294MM *Pro*
is cooled.  Without it a frame cannot be matched to a dark, and a dark library
cannot be validated: you cannot tell whether two frames were taken at the same
sensor temperature.  Since PHD2 both writes the file and can report the cooler,
it stamps its own frames -- so every consumer (acquisition, guiding, autofocus,
calibration) gets the keywords, not just whichever one asked for them.

**Bracketed, not mid-exposure.**  The obvious design -- a timer that fires at
t/2 and samples -- rests on an unverified assumption: that PHD2 answers RPC
*while* a capture is in flight.  If its RPC handling is serialised with the
capture loop, that query either returns after the frame (a post-exposure reading
wearing a mid-exposure label -- worse than no reading, because it looks right) or
delays the capture itself.  Sampling immediately before and immediately after
sidesteps the question entirely: both queries happen while the camera is idle.

For a 5s exposure on a cooled sensor's thermal mass the mean of the two is as
good as a true mid-point sample, and the pair carries something a single number
cannot -- ``CCDTDELT``, how far the sensor moved *during* the frame.  That
matters here: measured 2026-07-21, the cooler was at 100% power with the sensor
at 23.9C against a 5.0C setpoint, i.e. not regulating and drifting with ambient.
The delta is what makes that visible per-frame instead of hidden in an average.

Nothing here may raise: a missing or unwritable temperature must never fail an
exposure that otherwise succeeded.
"""

from imagers.cooling_stamp import stamp_cooling

__all__ = ["stamp_cooling"]
