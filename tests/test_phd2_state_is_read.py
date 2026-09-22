"""PHD2's state is read from PHD2, never recalled from what we sent it (#245).

The reads themselves. Removing the copies they replace needs the exposure-path
limit-frame reset removed with them, which is #250 against main; this branch adds
the reads that removal leaves behind, plus `get_exclude_region`, which only means
anything where an exclusion region can be set.

The defect this pins is not a wrong value. It is a *right* value describing the
wrong moment: a connector that answers from what it last sent is correct until
something else touches PHD2, and then it is confidently wrong with nothing to
distinguish the two. On the 2026-09-15/16 campaign that cost eleven of the twelve
hard preflight failures, and one arm of the programme never ran at all — the
limit frame had been changed in the DB, the change reaches PHD2 only at the next
`start_guiding`, and the gate was reading the last request on the wire.

So these tests drive the connector against a PHD2 that is allowed to disagree
with what it was told, which is the one thing a fake built from our own writes
can never do.
"""

from common.models.statuses import ImagerRoi
from phd2.phd2 import PHD2Connector

FULL_SENSOR = [0, 0, 8288, 5644]
DERIVED = [520, 0, 7760, 4812]
STRIP = [6363, 0, 1917, 4812]


class FakePhd2:
    """A PHD2 that accepts a setter and applies it only when told to.

    `pending` holds what was accepted; `applied` is what the getters report. Real
    PHD2 closes that gap on its own schedule, and the point of the separation is
    that a caller cannot tell the two apart without asking.
    """

    def __init__(self, limit_frame=None, exclude_region=None, lock_position=None, setpoint=None, cooler_on=False):
        self.applied = {
            "get_limit_frame": limit_frame,
            "get_exclude_region": exclude_region,
            "get_lock_position": lock_position,
        }
        self.pending: dict[str, object] = {}
        self.setpoint = setpoint
        self.cooler_on = cooler_on
        self.calls: list[str] = []

    def apply(self):
        self.applied.update(self.pending)
        self.pending.clear()

    def __call__(self, method, params=None, **kwargs):
        self.calls.append(method)
        if method in self.applied:
            return {"result": self.applied[method]}
        if method == "set_limit_frame":
            roi = params["roi"] if isinstance(params, dict) else params
            self.pending["get_limit_frame"] = roi
            return {"result": 0}
        if method == "set_exclude_region":
            roi = params["roi"] if isinstance(params, dict) else params
            self.pending["get_exclude_region"] = roi
            return {"result": 0}
        if method == "get_cooler_status":
            result = {"coolerOn": self.cooler_on}
            if self.cooler_on and self.setpoint is not None:
                result["setpoint"] = self.setpoint
            return {"result": result}
        raise AssertionError(f"unexpected call {method}")


def connector(fake: FakePhd2) -> PHD2Connector:
    """A connector with the protocol and none of the hardware."""
    c = object.__new__(PHD2Connector)
    c.call = fake
    c._connected = True
    return c


class TestTheGettersRead:
    def test_the_limit_frame_comes_from_phd2(self):
        c = connector(FakePhd2(limit_frame=DERIVED))
        roi = c.get_limit_frame()
        assert (roi.x, roi.y, roi.width, roi.height) == (520, 0, 7760, 4812)

    def test_a_rectangle_phd2_holds_is_not_re_conditioned(self):
        """Conditioning would move it, and it would stop describing the instrument.

        `ImagerRoi` snaps a rectangle to the camera's alignment constraints, which is
        right for one we are about to send and wrong for one PHD2 already holds: 520,0
        would come back as 527,1 and the reported state would be a place PHD2 is not.
        """
        conditioned = ImagerRoi(x=520, y=0, width=7760, height=4812)
        assert (conditioned.x, conditioned.y) == (527, 1), "guard: conditioning does move it"

        roi = connector(FakePhd2(limit_frame=DERIVED)).get_limit_frame()
        assert (roi.x, roi.y) == (520, 0)

    def test_no_limit_frame_reads_as_none(self):
        assert connector(FakePhd2(limit_frame=None)).get_limit_frame() is None

    def test_the_exclusion_region_comes_from_phd2(self):
        c = connector(FakePhd2(exclude_region=[2580, 0, 3783, 5644]))
        roi = c.get_exclude_region()
        assert (roi.x, roi.width) == (2580, 3783)

    def test_the_lock_position_comes_from_phd2(self):
        assert connector(FakePhd2(lock_position=[6067.6, 2560.5])).get_lock_position() == (6067.6, 2560.5)


class TestAcceptedIsNotApplied:
    """The gap that blocked the `none` arm for five consecutive cycles."""

    def test_a_set_that_has_not_landed_does_not_change_the_read(self):
        fake = FakePhd2(limit_frame=DERIVED)
        c = connector(fake)
        c._send_limit_frame(None)
        roi = c.get_limit_frame()
        assert roi is not None and roi.x == 520, "the read must report PHD2, not the request"

    def test_the_read_changes_once_phd2_applies_it(self):
        fake = FakePhd2(limit_frame=DERIVED)
        c = connector(fake)
        c._send_limit_frame(None)
        fake.apply()
        assert c.get_limit_frame() is None

    def test_restoring_means_reading_first(self):
        """Putting back what was there is a read the caller makes, not a memory it keeps."""
        fake = FakePhd2(limit_frame=STRIP)
        c = connector(fake)

        previous = c.get_limit_frame()
        assert (previous.x, previous.width) == (6363, 1917)

        c._send_limit_frame(ImagerRoi.verbatim(x=0, y=0, width=8288, height=5644))
        fake.apply()
        assert c.get_limit_frame().width == 8288

        c._send_limit_frame(previous)
        fake.apply()
        assert c.get_limit_frame().x == 6363

    def test_every_read_asks_phd2_again(self):
        """Two reads either side of a change must not agree by virtue of a cache."""
        fake = FakePhd2(limit_frame=DERIVED)
        c = connector(fake)
        first = c.get_limit_frame()
        fake.applied["get_limit_frame"] = STRIP
        second = c.get_limit_frame()
        assert first.x == 520 and second.x == 6363
        assert fake.calls.count("get_limit_frame") == 2
