import datetime
import json
import logging
import math
import os
import re
import sys
import time
from typing import NamedTuple

import astropy.units as u
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from astropy.coordinates import Angle
from matplotlib.patches import Patch

from common.corrections import Corrections, correction_phases
from common.mast_logging import init_log
from common.utils import Filer, function_name

logger = logging.Logger("mast.unit." + __name__)
filer = Filer(logger)
init_log(logger)

logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("PIL").setLevel(logging.WARNING)

ra_color = "blue"
dec_color = "green"


# Function to determine if the environment is interactive
def is_interactive():
    return sys.stdin.isatty()


class Point(NamedTuple):
    x: int
    y: float
    label: str


def plot_autofocus_analysis(
    result: "PS3FocusAnalysisResult",  # noqa: F821 # type: ignore
    folder: str | None = None,
    pixel_scale: float = 0.2612,
):
    op = function_name()

    if not result:
        logger.error(f"{op}: result is None")
        return

    if not result.has_solution:
        logger.error(f"{op}: result doesn't have a solution")
        return

    points: list[Point] = []
    positions: list[int] = []
    star_diameters: list[float] = []

    assert(result and result.focus_samples)
    for sample in result.focus_samples:
        if not (sample and sample.is_valid):
            continue
        assert sample.focus_position
        assert sample.star_rms_diameter_pixels
        points.append(
            Point(
                x=int(sample.focus_position),
                y=sample.star_rms_diameter_pixels,
                label=f"{sample.num_stars} stars",
            )
        )
        star_diameters.append(sample.star_rms_diameter_pixels)
        positions.append(int(sample.focus_position))

    # Define the focuser positions (X values)
    x = np.linspace(
        start=min(positions) - 100,
        stop=max(positions) + 100,
        num=abs(max(positions) - min(positions)) + 200,
    )

    # Calculate the diameters for each X value using the given equation
    assert(result.vcurve_a and result.vcurve_b and result.vcurve_c)
    star_diameter = np.sqrt(
        result.vcurve_a * x**2 + result.vcurve_b * x + result.vcurve_c
    )

    # Calculate the X-coordinate of the minimum using the formula X_min = -B / (2 * A)
    x_min = -result.vcurve_b / (2 * result.vcurve_a)

    # Calculate the diameter at the minimum X position
    diameter_min = np.sqrt(
        result.vcurve_a * x_min**2 + result.vcurve_b * x_min + result.vcurve_c
    )

    # Plot the V-curve
    plt.figure(figsize=(8, 6))
    plt.plot(x, star_diameter, label="Star diameters (RMS, pixels)", color=ra_color)

    # Add a red tick at the minimum X position on the X-axis
    plt.axvline(
        x_min,
        ymin=0,
        ymax=diameter_min,
        color="black",
        linestyle=":",
        label=f"Best focus: {int(x_min)} microns",
    )
    plt.scatter(x_min, diameter_min, color="red", zorder=5)

    # Add a black tick on the Y-axis at the minimum diameter
    min_diam_arcsec = diameter_min * pixel_scale
    plt.axhline(
        diameter_min,
        color="black",
        linestyle=":",
        label=f"Min. diam.: {diameter_min:.2f} px, {min_diam_arcsec:.2f} arcsec",
    )

    # Add green lines for tolerance
    assert(result.tolerance)
    x_left = x_min - result.tolerance
    x_right = x_min + result.tolerance
    y_max = np.max(star_diameter)
    # y_min = np.min(star_diameter)
    y_left = (
        np.sqrt(
            result.vcurve_a * x_left**2 + result.vcurve_b * x_left + result.vcurve_c
        )
        / y_max
    )
    y_right = (
        np.sqrt(
            result.vcurve_a * x_right**2 + result.vcurve_b * x_right + result.vcurve_c
        )
        / y_max
    )
    plt.axvline(
        x_left,
        ymin=0,
        ymax=y_left,
        color=dec_color,
        linestyle=":",
        label="2.5% diam. increase",
    )
    plt.axvline(x_right, ymin=0, ymax=y_right, color=dec_color, linestyle=":")

    # Add circles and labels at the specified points
    for point in points:
        plt.scatter(point.x, point.y, color=dec_color, s=25, zorder=5, marker="o")
        # Add label in small red font at NE corner
        plt.text(point.x + 5, point.y + 0.1, str(point.label), color="red", fontsize=10)

    # Add labels and title
    plt.title("Autofocus V-Curve")
    plt.xlabel("Focuser Position")
    plt.ylabel("Star Diameter (px)")

    tolerance_label = Patch(
        color="none", label=f"Tolerance: {result.tolerance:.1f} microns"
    )
    # Show grid and legend
    plt.grid(True)
    plt.legend(
        handles=plt.gca().get_legend_handles_labels()[0] + [tolerance_label],
        loc="upper right",
        framealpha=1,
    )

    if folder:
        file: str = os.path.join(folder, "vcurve.png")
        logger.info(f"{op}: saved plot in {file}")
        plt.savefig(file, format="png")
        time.sleep(2)
        filer.move_ram_to_shared(folder)

    plt.show()


def plot_phase_corrections(  # noqa: C901
    phase: str,  # one of ['sky', 'spec', 'guiding', 'acquisition']
    corrections: Corrections,
    file: str,  # .../<date>/Acquisitions/seq=<seq-number>,time=<start-time>,target=<target>
    ends_of_phases: list[datetime.datetime] | None = None,
    tolerances: dict | None = None,
):
    ra_guiding_rms: float = 0
    dec_guiding_rms: float = 0

    if not corrections or not corrections.sequence:
        return

    sequence = corrections.sequence
    if corrections.last_delta:
        sequence.append(corrections.last_delta)

    file_name_pattern = (
        r".*(?P<date>\d{4}-\d{2}-\d{2})\\Acquisitions\\"
        + r"seq=(?P<seq_number>\d+),time=(?P<start_time>\d{2}-\d{2}-\d{2}_\d{3}+),"
        + r"target=(?P<target>[^\\]+)"
    )
    match = re.search(file_name_pattern, file)
    if match:
        acq_date = match.group("date")
        # acq_seq_number = match.group("seq_number")
        acq_start_time = match.group("start_time")
        acq_target = match.group("target")
    else:
        raise ValueError(
            f"could not extract seq_number, start_time and target from path '{file}'"
        )

    if not sequence:
        logger.info(f"Empty sequence ({phase=}), not plotting")
        return

    start: datetime.datetime = sequence[0].time
    end: datetime.datetime = sequence[-1].time
    t = [(corr.time - start).seconds for corr in sequence]
    ra_deltas = [abs(corr.ra_delta) for corr in sequence]
    dec_deltas = [abs(corr.dec_delta) for corr in sequence]

    ra_rms_label = ""
    dec_rms_label = ""

    if phase == "guiding":
        square_sum = sum(x**2 for x in ra_deltas)
        ra_guiding_rms = math.sqrt(square_sum / len(ra_deltas))
        square_sum = sum(x**2 for x in dec_deltas)
        dec_guiding_rms = math.sqrt(square_sum / len(dec_deltas))

    matplotlib.use("Agg")
    plt.figure(figsize=(8, 6))
    plt.plot(t, ra_deltas, color=ra_color, label="Ra", marker="*")
    plt.plot(t, dec_deltas, color=dec_color, label="Dec", marker="*")

    if corrections.tolerance_dec == corrections.tolerance_ra:
        plt.axhline(
            y=corrections.tolerance_ra,
            color=ra_color,
            linestyle=":",
            label=f"Tolerance: {corrections.tolerance_ra:.2f}",
        )
    else:
        plt.axhline(
            y=corrections.tolerance_ra,
            color=ra_color,
            linestyle=":",
            label=f"RA tolerance: {corrections.tolerance_ra:.2f}",
        )
        plt.axhline(
            y=corrections.tolerance_dec,
            color=dec_color,
            linestyle=":",
            label=f"Dec tolerance: {corrections.tolerance_dec:.2f}",
        )

    with_label = True
    if ends_of_phases:
        for t in ends_of_phases:
            plt.axvline(
                x=(t - start).seconds,
                linestyle="--",
                color="black",
                label=("End of phase" if with_label else None),
            )
            if with_label:
                with_label = False

    if phase in ["guiding", "acquisition"]:
        ra_rms_label = Patch(color="none", label=f"RA  RMS: {ra_guiding_rms:.2f}")
        dec_rms_label = Patch(color="none", label=f"Dec RMS: {dec_guiding_rms:.2f}")

    if phase == "acquisition" and tolerances:
        if "sky" in tolerances and "ra" in tolerances["sky"]:
            plt.axhline(
                y=tolerances["sky"]["ra"],
                color="maroon",
                linestyle=":",
                label=f"'sky' tolerance ({tolerances['sky']['ra']} arcsec)",
            )
        if "spec" in tolerances and "ra" in tolerances["spec"]:
            plt.axhline(
                y=tolerances["spec"]["ra"],
                color="maroon",
                linestyle="-.",
                label=f"'spec' tolerance ({tolerances['spec']['ra']} arcsec)",
            )

    if start.day == end.day:
        start_time = f"{start.time().strftime('%H:%M:%S.%f')[:11]}"
        end_time = f"{end.time().strftime('%H:%M:%S.%f')[:11]}"
    else:
        start_time = Patch(color="none", label=f"{start}")
        end_time = Patch(color="none", label=f"{end}")

    plt.xlabel(f"Delta time (sec), Time span: {start_time}, {end_time}")
    plt.ylabel("Corrections in arcsec (log. scale) ")
    plt.yscale("log")

    title = f"Acquisition: {acq_date} {acq_start_time.replace('-', ':').replace('_', '.')} UT\n"
    title += f" target: {acq_target}\n"
    if phase != "acquisition":
        title += f"  phase: {phase}"
    plt.title(f"{title}", loc="left")

    labels = []
    if phase in ["guiding", "acquisition"]:
        labels = labels + [ra_rms_label, dec_rms_label]
    plt.legend(
        handles=plt.gca().get_legend_handles_labels()[0] + labels,
        loc="upper right",
        framealpha=0.7,
    )

    plt.grid()

    file = file.replace(".json", ".png")
    plt.savefig(file, format="png")
    # plt.close()
    logger.info(f"plot saved to '{file}'")


def plot_acquisition_corrections(acquisition_folder: str | None = None):  # noqa: C901
    """
    Plots the existing corrections.json files underneath a given Acquisition folder

    :param acquisition_folder: An Acquisition folder.  If not given, finds the latest available one.
    :return:
    """
    op = function_name()

    def has_corrections(_folder: str) -> bool:
        return any(os.path.exists(os.path.join(_folder, _phase, "corrections.json")) for _phase in correction_phases)

    acquisition_top = None
    if acquisition_folder is not None:
        acquisition_top = acquisition_folder

    if acquisition_top is None:
        while acquisition_top is None:
            latest_acquisition_folders = filer.find_latest(
                filer.shared.root, pattern="*,target=*"
            )
            if not latest_acquisition_folders:
                logger.error(
                    f"{op}: Could not find acquisition folders under '{filer.shared.root}'"
                )
                return
            for folder in latest_acquisition_folders:
                if has_corrections(folder):
                    acquisition_top = folder
                    break

    if acquisition_top is None:
        logger.error(
            f"{op}: Could not find an acquisition folder with corrections under '{filer.shared.root}'"
        )
        return

    combined_corrections: Corrections | None = None
    end_of_phase: list[datetime.datetime] = []
    tolerances = {}

    # for phase in ["sky", "spec", "guiding"]:
    for phase in ["sky", "spec"]:
        file = os.path.join(acquisition_top, phase, "corrections.json")
        if not os.path.isfile(file):
            continue

        try:
            with open(file) as fp:
                corrections: Corrections = Corrections.from_dict(json.load(fp))
        except Exception as e:
            logger.error(f"{op}: Could not get corrections from {file} ({e=})")
            continue

        if not corrections:
            continue
        sequence = corrections.sequence
        if phase != "guiding" and corrections.last_delta:
            sequence.append(corrections.last_delta)

        if combined_corrections is None:
            combined_corrections = Corrections(
                phase="acquisition",
                target_ra=corrections.target_ra,
                target_dec=corrections.target_dec,
                tolerance_ra=corrections.tolerance_ra,
                tolerance_dec=corrections.tolerance_dec,
            )
        tolerances[phase] = {
            "ra": corrections.tolerance_ra,
            "dec": corrections.tolerance_dec,
        }
        combined_corrections.sequence += sequence
        end_of_phase.append(sequence[0].time)

    if combined_corrections is not None:
        plot_phase_corrections(
            phase="acquisition",
            corrections=combined_corrections,
            file=os.path.join(acquisition_top, "corrections.json"),
            ends_of_phases=end_of_phase,
            tolerances=tolerances,
        )


#
# Unit tests
#
class FocusSample:
    def __init__(
        self,
        is_valid: bool,
        focus_position: int,
        num_stars: int,
        star_rms_diameter_pixels: float,
    ):
        self.is_valid = is_valid
        self.focus_position = focus_position
        self.num_stars = num_stars
        self.star_rms_diameter_pixels = star_rms_diameter_pixels


class DummyResult:

    def __init__(self):
        self.has_solution: bool = True
        self.best_focus_position: int = 27444
        self.tolerance: float = 20
        self.vcurve_a: float = 0.003969732449914025
        self.vcurve_b: float = -218.95312265253094
        self.vcurve_c: float = 3019252.867956934
        self.focus_samples = [
            FocusSample(
                is_valid=True,
                focus_position=27444,
                num_stars=12,
                star_rms_diameter_pixels=14.059341433692959,
            ),
            FocusSample(
                is_valid=True,
                focus_position=27494,
                num_stars=13,
                star_rms_diameter_pixels=12.504629812575239,
            ),
            FocusSample(
                is_valid=True,
                focus_position=27544,
                num_stars=14,
                star_rms_diameter_pixels=11.868161311662165,
            ),
            FocusSample(
                is_valid=True,
                focus_position=27594,
                num_stars=18,
                star_rms_diameter_pixels=10.85730885089308,
            ),
        ]


class Coord:
    def __init__(self, ra: float, dec: float):
        self.ra = Angle(ra * u.hourangle) # type: ignore
        self.dec = Angle(dec * u.deg) # type: ignore


class DummyStatus:

    def __init__(self):
        self.is_running = False
        self.has_solution: bool = True
        self.analysis_result = DummyResult()


def test_corrections_plot():

    start = datetime.datetime.now()
    dt = [
        0,
        32,
        65,
        96,
        128,
        159,
        190,
        221,
        252,
        285,
        316,
        347,
        378,
        411,
        442,
        473,
        506,
        539,
        572,
        603,
    ]
    d_ra = [
        10.5,
        9.662,
        9.144,
        8.794,
        8.589,
        6.990,
        6.453,
        5.682,
        4.947,
        4.780,
        4.429,
        3.444,
        3.005,
        2.457,
        2.294,
        1.684,
        1.151,
        0.904,
        0.896,
        0.3,
    ]
    d_dec = [
        2.3,
        1.937,
        1.555,
        1.348,
        1.199,
        0.593,
        0.521,
        0.468,
        0.434,
        0.273,
        0.27,
        0.27,
        0.27,
        0.27,
        0.27,
        0.27,
        0.27,
        0.27,
        0.27,
        0.27,
    ]
    dummy = {
        "target": {
            "ra": 14.5,
            "dec": 32.75,
        },
        "sequence": [],
    }
    for i in range(len(dt)):
        dummy["sequence"].append(
            {
                "time": (start + datetime.timedelta(seconds=dt[i])).isoformat(),
                "ra_delta": d_ra[i],
                "dec_delta": d_dec[i],
            }
        )

    with open("C:\\Temp\\corrections.json", "w") as f:
        json.dump(dummy, f, indent=2)
    plot_acquisition_corrections("C:\\Temp\\corrections.json")


if __name__ == "__main__":
    # acq_folder = filer.find_latest(filer.shared.root, pattern='*,target=*', qualifier=os.path.isdir)
    acq_folder = r"D:\tmp\2024-12-05\Acquisitions\seq=0025,time=18-13-03_987,target=1.42677311977099,23.5115091209584"
    plot_acquisition_corrections(acq_folder)
    # plot_autofocus_analysis(DummyResult(), 'C:\\Temp')
    # test_corrections_plot()
    pass
