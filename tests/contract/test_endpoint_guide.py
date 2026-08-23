"""`docs/adding-an-endpoint.md` names every check and every tier (#42).

The guide is the entry point for anyone adding a route, so its failure mode is silence: a
new static check lands, nobody adding an endpoint hears about it, and the guide reads as
complete while covering nine of ten. That is the same defect this suite exists to prevent
one layer down, so it gets the same treatment -- set equality against what the tree actually
contains, rather than a reviewer remembering.

**Completeness only.** This cannot tell whether a paragraph is *true*; it can only tell that
nothing is missing. Changing what a check enforces still means editing the prose by hand.
"""

from __future__ import annotations

from pathlib import Path

from common.endpoints import Tier

CONTRACT_DIR = Path(__file__).parent
GUIDE = CONTRACT_DIR.parent.parent / "docs" / "adding-an-endpoint.md"

# The guide describes what each check enforces in prose, not by filename, so a module is
# "covered" when its stem appears somewhere in the text. Two modules are deliberately absent
# from that requirement: this one, and the guide is not a place to document its own test.
SELF = {"test_endpoint_guide"}


def _guide_text() -> str:
    assert GUIDE.exists(), f"the endpoint guide is gone: {GUIDE}"
    return GUIDE.read_text(encoding="utf-8")


def test_the_guide_exists_and_says_something():
    text = _guide_text()
    assert len(text.splitlines()) > 50, "the guide has been truncated to a stub"


def test_every_tier_is_documented():
    """A tier a reader cannot look up is a tier they will guess at."""
    text = _guide_text()
    missing = sorted(tier.value for tier in Tier if tier.value not in text)
    assert not missing, f"docs/adding-an-endpoint.md does not mention these tiers: {missing}"


def test_every_contract_check_is_documented():
    """Adding a check without a line in the guide fails here, in the same change."""
    text = _guide_text()
    modules = {path.stem for path in CONTRACT_DIR.glob("test_*.py") if path.stem not in SELF}
    # Matched on the filename, not on a paraphrase: when a check goes red, the reader
    # needs the file to open, so the guide carries a module-to-refusal table and this
    # asserts every module has a row.
    undocumented = sorted(stem for stem in modules if f"{stem}.py" not in text)
    assert not undocumented, (
        f"docs/adding-an-endpoint.md does not cover these checks: {undocumented} -- add a line saying what each one refuses"
    )
