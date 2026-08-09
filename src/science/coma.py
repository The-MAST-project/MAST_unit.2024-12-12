import matplotlib.pyplot as plt
import numpy as np
from astropy.io import fits
from astropy.stats import SigmaClip
from photutils.aperture import EllipticalAperture
from photutils.background import Background2D, MedianBackground
from photutils.segmentation import SourceCatalog, detect_sources, detect_threshold


def find_optical_center(
    fits_filename,
    nsigma=2.0,  # threshold detection (in sigma)
    npixels=5,  # minimum number of connected pixels for a source
    box_size=50,  # size of the box for background estimation
    filter_sources=True,
    min_area=10,  # min area (pixels) for a valid source
    max_area=1e6,  # max area (pixels) for a valid source
    plot_results=True,
):
    """
    Open a FITS file containing an astronomical image and attempt to use
    the comatic elliptical shapes of sources to find an approximate
    optical center.

    Parameters
    ----------
    fits_filename : str
        Path to the FITS file.
    nsigma : float
        Number of sigma above background for threshold detection.
    npixels : int
        Minimum number of connected pixels to be considered a source.
    box_size : int
        Size (in pixels) of the box for 2D background estimation.
    filter_sources : bool
        Whether to filter out sources smaller/bigger than specified area.
    min_area : float
        Minimum area in pixels for a valid source.
    max_area : float
        Maximum area in pixels for a valid source.
    plot_results : bool
        Whether to produce a matplotlib figure showing detected sources
        and the computed center.

    Returns
    -------
    (center_x, center_y) : tuple of float
        The computed optical center in image (pixel) coordinates.
    """

    # -----------------------------
    # 1) Load image from FITS file
    # -----------------------------
    with fits.open(fits_filename) as hdul:
        data = hdul[0].data.astype(float)

    # -----------------------------
    # 2) Background subtraction
    # -----------------------------
    sigma_clip = SigmaClip(sigma=3.0)
    bkg_estimator = MedianBackground()
    bkg = Background2D(
        data,
        box_size=(box_size, box_size),
        filter_size=(3, 3),
        sigma_clip=sigma_clip,
        bkg_estimator=bkg_estimator,
    )
    data_bkg_sub = data - bkg.background

    # -----------------------------
    # 3) Detect and deblend sources
    # -----------------------------
    threshold = detect_threshold(data_bkg_sub, nsigma=nsigma)
    segm = detect_sources(data_bkg_sub, threshold, npixels=npixels)

    if segm is None:
        print("No sources detected. Consider lowering nsigma or npixels.")
        return

    # segm_deblend = deblend_sources(
    #     data_bkg_sub, segm, npixels=npixels,
    #     nlevels=32, contrast=0.01
    # )

    # -----------------------------
    # 4) Measure source properties
    # -----------------------------
    # catalog = SourceCatalog(data_bkg_sub, segm_deblend,
    catalog = SourceCatalog(data_bkg_sub, segm, background=bkg.background, error=None)
    tbl = catalog.to_table()

    # Optionally filter out spurious sources
    if filter_sources:
        # 'area' is the measured source area in pixels
        area = tbl["area"].size
        valid = (area >= min_area) & (area <= max_area)
        tbl = tbl[valid]

    if len(tbl) == 0:
        print("No valid sources after filtering. Adjust min_area/max_area.")
        return

    # Extract relevant columns
    x_centroids = tbl.columns["xcentroid"]
    y_centroids = tbl.columns["ycentroid"]
    orientations = tbl.columns["orientation"]  # in radians, CCW from +x
    major_axes = tbl.columns["semimajor_sigma"]
    minor_axes = tbl.columns["semiminor_sigma"]

    # ---------------------------------------------------------
    # 5) Compute approximate optical center using a simple model
    # ---------------------------------------------------------
    #
    # We assume the major-axis orientation points radially away (or toward)
    # the optical center. That means for each source i:
    #
    #   (y_i - Y) / (x_i - X) = tan(orientation_i)
    #
    # Rearrange: y_i - slope_i*x_i = Y - slope_i*X
    # where slope_i = tan(orientation_i).
    # This is linear in X, Y. We solve in a least-squares sense.

    slopes = np.tan(orientations)

    # Build the linear system A * [X, Y]^T = C
    A = np.column_stack((-slopes, np.ones_like(slopes)))
    C = y_centroids - slopes * x_centroids

    # Solve for center_x, center_y
    # sol, residuals, rank, s = np.linalg.lstsq(A, C, rcond=None)
    # center_x, center_y = sol

    # -----------------------------
    # 6) Plot results if requested
    # -----------------------------
    if plot_results:
        fig, ax = plt.subplots(figsize=(8, 8))

        # Show the background-subtracted image
        vmin = np.percentile(data_bkg_sub, 5)
        vmax = np.percentile(data_bkg_sub, 99)
        ax.imshow(data_bkg_sub, origin="lower", cmap="gray", vmin=vmin, vmax=vmax)
        ax.set_title("Detected Sources & Estimated Optical Center")
        ax.set_xlabel("X (pix)")
        ax.set_ylabel("Y (pix)")

        # Overlay sources
        for i in range(len(tbl)):
            x0 = x_centroids[i]
            y0 = y_centroids[i]
            orient = orientations[i]
            a = 2.5 * major_axes[i]  # factor to scale the ellipse size
            b = 2.5 * minor_axes[i]

            # Mark source centroid
            ax.plot(x0, y0, marker="o", markersize=3, color="red")
            print(f"{i:3}: {x0:8.3f} {y0:8.3f}")

            # Draw an elliptical "contour"
            aper = EllipticalAperture((x0, y0), a.value, b.value, theta=orient)
            aper.plot(ax=ax, color="yellow", lw=1)

            # Draw major-axis line from -a to +a along orientation
            dx = a * np.cos(orient)
            dy = a * np.sin(orient)
            ax.plot(
                [x0 - dx.value, x0 + dx.value],
                [y0 - dy.value, y0 + dy.value],
                color="yellow",
                lw=2,
            )

        # Mark the estimated optical center
        # ax.plot(center_x, center_y, marker='+', color='magenta', ms=15, mew=2)

        plt.tight_layout()
        plt.show()

    # -------------------------------------
    # 7) Return the computed optical center
    # -------------------------------------
    # return (center_x, center_y)


if __name__ == "__main__":
    # Example usage:
    center = find_optical_center(
        "c:/temp/spec.fits",
        nsigma=2.0,
        npixels=5,
        box_size=50,
        filter_sources=True,
        min_area=10,
        max_area=1e5,
        plot_results=True,
    )
    print("Estimated optical center:", center)
