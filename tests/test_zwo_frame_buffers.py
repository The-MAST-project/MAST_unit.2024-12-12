"""The ZWO readout into preallocated buffers (`imager.reuse_frame_buffers`), and the ring behind it.

The SDK is a fake: the real `ASIGetDataAfterExp` is replaced by one that copies a known payload through the pointer it is
given, so what is checked is that the data lands in the buffer, that nothing is allocated per frame, that two buffers
alternate, and that a frame handed to the saver is its own.
"""

from __future__ import annotations

import ctypes
import importlib
from types import SimpleNamespace

import numpy as np
import pytest

from imagers.frame_ring import FrameRing

zwo = importlib.import_module("zwo")

H, W = 6, 8


class ASIError(Exception):
    pass


class FakeSdk:
    """pyzwoasi as `zwo` sees it: `getDataAfterExp` (allocating) and `pyzwoasi.lib.ASIGetDataAfterExp` (into a pointer)."""

    def __init__(self, with_lib: bool = True, code: int = 0):
        self.payload = b""
        self.allocating_calls = 0
        self.direct_calls = 0
        self.code = code
        self.ASIError = ASIError
        if with_lib:
            self.pyzwoasi = SimpleNamespace(lib=SimpleNamespace(ASIGetDataAfterExp=self._direct))

    def exposure(self, value: int, dtype=np.uint16, count: int | None = None) -> None:
        data = np.full(H * W if count is None else count, value, dtype=dtype)
        self.payload = data.tobytes()

    def getDataAfterExp(self, cam_id, bufferSize):  # noqa: N802, N803 -- the SDK's names
        self.allocating_calls += 1
        return self.payload.ljust(bufferSize, b"\0")[:bufferSize]

    def _direct(self, cam_id, pointer, size):
        self.direct_calls += 1
        if self.code == 0:
            ctypes.memmove(pointer, self.payload, min(len(self.payload), size))
        return self.code


def _backend(monkeypatch, sdk: FakeSdk, reuse: bool, fmt: str = "raw16"):
    class _Zwo(zwo.ZWOImager):
        def __del__(self):
            """Not the real one: it closes a camera this instance never opened."""

    backend = object.__new__(_Zwo)
    backend.cam_id = 0
    unit_conf = SimpleNamespace(imager=SimpleNamespace(reuse_frame_buffers=reuse))
    backend.parent_imager = SimpleNamespace(unit=SimpleNamespace(unit_conf=unit_conf))
    backend.latest_settings = SimpleNamespace(roi=SimpleNamespace(height=H, width=W), format=fmt)
    monkeypatch.setattr(zwo, "zwoasi", sdk)
    return backend


# ------------------------------------------------------------------------------------------------ the ring


class TestFrameRing:
    def test_two_buffers_alternate(self):
        ring = FrameRing(depth=2)
        a, b, c, d = (ring.next(H, W, np.uint16) for _ in range(4))

        assert a is not b and c is a and d is b  # the third frame reuses the first buffer
        assert not np.shares_memory(a, b)

    def test_a_buffer_is_writable_and_contiguous(self):
        frame = FrameRing().next(H, W, np.uint16)

        assert frame.flags.writeable and frame.flags.c_contiguous and frame.shape == (H, W)

    def test_a_new_shape_or_type_reallocates_only_that_slot(self):
        ring = FrameRing()
        a = ring.next(H, W, np.uint16)  # slot 0
        b = ring.next(H, W, np.uint16)  # slot 1
        assert ring.next(H, W, np.uint16) is a  # slot 0 again: the same memory

        resized = ring.next(H + 2, W, np.uint16)  # slot 1, now a different shape

        assert resized.shape == (H + 2, W) and resized is not b
        assert ring.next(H, W, np.uint16) is a  # slot 0 was not touched
        assert ring.next(H + 2, W, np.uint8).dtype == np.uint8  # slot 1 again, a new type

    def test_a_ring_of_one_is_refused(self):
        with pytest.raises(ValueError, match="hand out the frame it was just asked to keep"):
            FrameRing(depth=1)

    def test_clear_lets_the_buffers_go(self):
        ring = FrameRing()
        first = ring.next(H, W, np.uint16)

        ring.clear()

        assert ring.next(H, W, np.uint16) is not first


# ------------------------------------------------------------------------------------------------ the readout


def test_by_default_each_frame_is_a_fresh_array_from_the_allocating_call(monkeypatch):
    sdk = FakeSdk()
    backend = _backend(monkeypatch, sdk, reuse=False)

    sdk.exposure(7)
    first = backend._read_frame()
    sdk.exposure(9)
    second = backend._read_frame()

    assert (sdk.allocating_calls, sdk.direct_calls) == (2, 0)  # as it always was
    assert first.shape == (H, W) and first.dtype == np.uint16
    assert (first == 7).all() and (second == 9).all()  # the first frame is still intact: nothing is reused
    assert not np.shares_memory(first, second)


def test_with_the_setting_frames_are_read_into_two_alternating_buffers(monkeypatch):
    sdk = FakeSdk()
    backend = _backend(monkeypatch, sdk, reuse=True)

    frames = []
    for value in (1, 2, 3):
        sdk.exposure(value)
        frames.append(backend._read_frame())

    assert (sdk.allocating_calls, sdk.direct_calls) == (0, 3)  # no allocation per frame
    assert np.shares_memory(frames[0], frames[2]) and not np.shares_memory(frames[0], frames[1])
    assert (frames[1] == 2).all() and (frames[2] == 3).all()
    assert (frames[2] == 3).all()  # frame 3 overwrote frame 1's buffer: the documented lifetime


def test_raw8_frames_are_bytes(monkeypatch):
    sdk = FakeSdk()
    backend = _backend(monkeypatch, sdk, reuse=True, fmt="raw8")
    sdk.exposure(5, dtype=np.uint8)

    frame = backend._read_frame()

    assert frame.dtype == np.uint8 and (frame == 5).all()


def test_a_short_readout_leaves_zeros_not_the_last_frame(monkeypatch):
    sdk = FakeSdk()
    backend = _backend(monkeypatch, sdk, reuse=True)
    sdk.exposure(9)
    backend._read_frame()
    backend._read_frame()  # both buffers hold a full frame of 9s

    sdk.exposure(4, count=H * W // 2)  # the SDK writes half of what the buffer holds
    frame = backend._read_frame()

    flat = frame.reshape(-1)
    assert (flat[: H * W // 2] == 4).all() and (flat[H * W // 2 :] == 0).all()


def test_without_the_direct_sdk_call_the_copy_goes_into_the_buffer(monkeypatch):
    sdk = FakeSdk(with_lib=False)
    backend = _backend(monkeypatch, sdk, reuse=True)
    sdk.exposure(6)

    frame = backend._read_frame()

    assert sdk.allocating_calls == 1 and (frame == 6).all()


def test_an_sdk_error_code_is_raised(monkeypatch):
    sdk = FakeSdk(code=11)
    backend = _backend(monkeypatch, sdk, reuse=True)

    with pytest.raises(ASIError, match="Error code: 11"):
        backend._read_frame()


def test_turning_the_setting_off_lets_the_buffers_go(monkeypatch):
    sdk = FakeSdk()
    backend = _backend(monkeypatch, sdk, reuse=True)
    sdk.exposure(1)
    backend._read_frame()
    assert backend._frame_ring is not None

    backend.parent_imager.unit.unit_conf.imager.reuse_frame_buffers = False
    backend._read_frame()

    assert backend._frame_ring is None


# ------------------------------------------------------------------------------------------------ the saver


def test_the_saver_is_handed_its_own_copy_of_a_reused_buffer(monkeypatch):
    sdk = FakeSdk()
    backend = _backend(monkeypatch, sdk, reuse=True)
    sdk.exposure(8)
    frame = backend._read_frame()

    given = backend._frame_for_saver(frame)

    assert given is not frame and not np.shares_memory(given, frame) and (given == 8).all()


def test_a_fresh_frame_is_not_copied_for_the_saver(monkeypatch):
    sdk = FakeSdk()
    backend = _backend(monkeypatch, sdk, reuse=False)
    sdk.exposure(8)
    frame = backend._read_frame()

    assert backend._frame_for_saver(frame) is frame


def test_the_saver_writes_the_frame_it_was_given(monkeypatch):
    backend = _backend(monkeypatch, FakeSdk(), reuse=False)
    seen = {}
    import imagers.saving as saving

    monkeypatch.setattr(saving, "save_to_fits_file", lambda b, image=None: seen.update(backend=b, image=image))
    frame = np.arange(4, dtype=np.uint16).reshape(2, 2)
    backend.image_was_saved = False
    backend.image_saved_event = SimpleNamespace(set=lambda: None)

    backend.save_in_thread(frame)

    assert seen["backend"] is backend and seen["image"] is frame and backend.image_was_saved
