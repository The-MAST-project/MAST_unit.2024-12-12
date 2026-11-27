"""The UnitConfig imager/guider coherence rule (``validate_unit_config``): the in-house
``mast`` guider and the ``zwo`` imager are one stack, so a config must pair them together or
not at all. Exercised directly on the validator -- a full ``UnitConfig`` needs a dozen
sub-configs, and the rule reads only ``imager.imager_type`` and ``guider.method``. The same
rule guards startup in ``app.verify_imager_guider_coherence``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.config.unit import UnitConfig


def _run(imager_type: str, method: str):
    """Invoke the model validator against a stand-in carrying just the two fields it reads.

    ``validate_unit_config`` is an ``@model_validator(mode="after")`` -- a plain method that
    takes ``self`` and returns it (or raises). Calling it unbound with a stub exercises the
    rule without constructing a whole UnitConfig.
    """
    stub = SimpleNamespace(
        imager=SimpleNamespace(imager_type=imager_type),
        guider=SimpleNamespace(method=method),
    )
    return UnitConfig.validate_unit_config(stub)


@pytest.mark.parametrize(
    "imager_type,method",
    [
        ("zwo", "mast"),  # the coherent in-house stack
        ("ascom", "phd2"),  # the PHD2 stack -- neither zwo nor mast
        ("phd2", "phd2"),
        ("ascom", "solving"),  # solving guider on a non-zwo imager
        ("ascom:ASCOM.ASICamera2.Camera", "phd2"),  # the prefixed ascom type is not 'zwo'
    ],
)
def test_coherent_pairs_pass(imager_type, method):
    stub = _run(imager_type, method)
    assert stub is not None  # returns self, unchanged


@pytest.mark.parametrize(
    "imager_type,method",
    [
        ("zwo", "phd2"),  # zwo imager, guider that will not drive it
        ("zwo", "solving"),
        ("ascom", "mast"),  # mast guider, no zwo frames to track
        ("phd2", "mast"),
    ],
)
def test_incoherent_pairs_are_rejected(imager_type, method):
    with pytest.raises(ValueError, match="incoherent imager/guider configuration"):
        _run(imager_type, method)
