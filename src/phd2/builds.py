"""Which MAST PHD2 build is running, as a number.

MAST builds carry a monotonic counter in PHD2's sub-version, `2.6.14dev1mastbuild6` (see
the vault's "PHD2 limit frame mechanism and MAST build provenance"). A feature that needs
build N compares numbers: as strings, `dev1mastbuild10` sorts before `dev1mastbuild6`.
"""

from __future__ import annotations

import re

_MASTBUILD = re.compile(r"mastbuild(\d+)$")

#: GuideStep carries `PrimaryDx`/`PrimaryDy`, the primary star's own offset from the lock.
PRIMARY_OFFSET_MASTBUILD = 6


def mastbuild_number(sub_version: str) -> int | None:
    """The `mastbuildN` counter in PHD2's sub-version, or None for a build that has none
    (a stock upstream build, or one that never reported its version)."""
    match = _MASTBUILD.search(sub_version or "")
    return int(match.group(1)) if match else None
