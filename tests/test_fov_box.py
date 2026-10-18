"""The box focus_on_star measures in: size, placement, binning, and where its center comes from."""

from types import SimpleNamespace

from common.config.rois import FcuVersion
from imaging.fov import focus_center_bin1, fov_box, fov_box_size, frame_shape_bin1

ASI = (5644, 8288)  # (ny, nx)


def test_ten_percent_is_linear():
    assert fov_box_size(ASI, 10.0) == (829, 564)


def test_box_is_centred_on_the_center():
    x0, x1, y0, y1 = fov_box(ASI, (4000.0, 2800.0), 10.0)
    assert (x1 - x0, y1 - y0) == (829, 564)
    assert abs((x0 + x1) / 2 - 4000) <= 1 and abs((y0 + y1) / 2 - 2800) <= 1


def test_a_center_near_an_edge_shifts_the_box_and_keeps_its_size():
    x0, x1, y0, y1 = fov_box(ASI, (100.0, 5600.0), 10.0)
    assert (x0, y1) == (0, 5644)
    assert (x1 - x0, y1 - y0) == (829, 564)


def test_binning_two_halves_the_box():
    x0, x1, y0, y1 = fov_box(ASI, (4000.0, 2800.0), 10.0, binning=2)
    bx0, bx1, by0, by1 = fov_box(ASI, (4000.0, 2800.0), 10.0)
    assert (x0, y0) == (bx0 // 2, by0 // 2)
    assert x1 - x0 in (414, 415) and y1 - y0 == 282


def test_the_focus_center_is_the_fibre_pixel():
    conf = SimpleNamespace(guiding=SimpleNamespace(rois={FcuVersion.v2: SimpleNamespace(fiber_x=4100, fiber_y=2750)}))
    assert focus_center_bin1(conf, FcuVersion.v2) == (4100.0, 2750.0)
    assert focus_center_bin1(conf, FcuVersion.v1) is None


def test_frame_shape_falls_back_to_the_asi294():
    assert frame_shape_bin1(SimpleNamespace(camera_x_size=None, camera_y_size=None)) == ASI
    assert frame_shape_bin1(SimpleNamespace(camera_x_size=4144, camera_y_size=2822)) == (2822, 4144)
