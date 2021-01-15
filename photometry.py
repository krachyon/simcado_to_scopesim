from typing import Tuple

import numpy as np
import photutils
from astropy.modeling.fitting import LevMarLSQFitter
from astropy.nddata import NDData
from astropy.stats import sigma_clipped_stats, gaussian_sigma_to_fwhm
from astropy.table import Table
from image_registration.fft_tools import upsample_image
from photutils import EPSFBuilder
from photutils.background import MMMBackground, MADStdBackgroundRMS
from photutils.detection import IRAFStarFinder, DAOStarFinder
from photutils.psf import BasicPSFPhotometry, extract_stars, DAOGroup, IntegratedGaussianPRF,\
    IterativelySubtractedPSFPhotometry
from scipy.spatial import cKDTree
import astropy

import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

def do_photometry_basic(image: np.ndarray, σ_psf: float) -> Tuple[Table, np.ndarray]:
    """
    Find stars in an image

    :param image: The image data you want to find stars in
    :param σ_psf: expected deviation of PSF
    :return: tuple result table, residual image
    """
    bkgrms = MADStdBackgroundRMS()

    std = bkgrms(image)

    iraffind = IRAFStarFinder(threshold=3 * std, sigma_radius=σ_psf,
                              fwhm=σ_psf * gaussian_sigma_to_fwhm,
                              minsep_fwhm=2, roundhi=5.0, roundlo=-5.0,
                              sharplo=0.0, sharphi=2.0)
    daogroup = DAOGroup(0.1 * σ_psf * gaussian_sigma_to_fwhm)

    mmm_bkg = MMMBackground()

    # my_psf = AiryDisk2D(x_0=0., y_0=0.,radius=airy_minimum)
    # psf_model = prepare_psf_model(my_psf, xname='x_0', yname='y_0', fluxname='amplitude',renormalize_psf=False)
    psf_model = IntegratedGaussianPRF(sigma=σ_psf)
    # psf_model = AiryDisk2D(radius = airy_minimum)#prepare_psf_model(AiryDisk2D,xname ="x_0",yname="y_0")
    # psf_model = Moffat2D([amplitude, x_0, y_0, gamma, alpha])

    # photometry = IterativelySubtractedPSFPhotometry(finder=iraffind, group_maker=daogroup,
    #                                                bkg_estimator=mmm_bkg, psf_model=psf_model,
    #                                                fitter=LevMarLSQFitter(),
    #                                                niters=2, fitshape=(11,11))
    photometry = BasicPSFPhotometry(finder=iraffind, group_maker=daogroup,
                                    bkg_estimator=mmm_bkg, psf_model=psf_model,
                                    fitter=LevMarLSQFitter(), aperture_radius=11.0,
                                    fitshape=(11, 11))

    result_table = photometry.do_photometry(image)
    return result_table, photometry.get_residual_image()


###
# magic parameters
clip_sigma = 3.0
threshold_factor = 3.
box_size = 10
cutout_size = 50  # TODO PSF is pretty huge, right?
fwhm_guess = 2.5
oversampling = 4
epsfbuilder_iters = 3
separation_factor = 1.  # TODO adapt to sensible value

###

# TODO how to
#  - find the best star candidates that are isolated for the PSF estimation?
#  - Guess the FWHM for the starfinder that is used in the Photometry pipeline? Do we need that if we have a custom PSF?

def cut_edges(peak_table: Table, box_size: int, image_size: int) -> Table:
    half = box_size / 2
    x = peak_table['x']
    y = peak_table['y']
    mask = ((x > half) & (x < (image_size - half)) & (y > half) & (y < (image_size - half)))
    return peak_table[mask]


def cut_close_stars(peak_table: Table, cutoff_dist: float) -> Table:
    peak_table['nearest'] = 0.
    x_y = np.array((peak_table['x'], peak_table['y'])).T
    lookup_tree = cKDTree(x_y)
    for row in peak_table:
        # find the second nearest neighbour, first one will be the star itself...
        dist, _ = lookup_tree.query((row['x'], row['y']), k=[2])
        row['nearest'] = dist[0]

    peak_table = peak_table[peak_table['nearest'] > cutoff_dist]
    return peak_table


def FWHM_estimate(psf: photutils.psf.EPSFModel) -> float:
    """
    Use a 2D symmetric gaussian fit to estimate the FWHM of a empirical psf
    :param model: EPSFModel instance that was derived
    :return: FWHM in pixel coordinates, takes into account oversampling parameter of EPSF
    """
    from astropy.modeling import fitting
    from astropy.modeling.functional_models import Gaussian2D

    # Not sure if this would work for non-quadratic images
    assert (psf.psfmodel.data.shape[0] == psf.psfmodel.data.shape[1])
    assert (psf.psfmodel.oversampling[0] == psf.psfmodel.oversampling[1])
    dim = psf.psfmodel.data.shape[0]
    center = int(dim / 2)
    gauss_in = Gaussian2D(x_mean=center, y_mean=center, x_stddev=5, y_stddev=5)

    # force a symmetric gaussian
    gauss_in.y_stddev.tied = lambda model: model.x_stddev

    x, y = np.mgrid[:dim, :dim]
    gauss_out = fitting.LevMarLSQFitter()(gauss_in, x, y, psf.psfmodel.data)

    # have to divide by oversampling to get back to original scale
    return gauss_out.x_fwhm / psf.psfmodel.oversampling[0]


def make_stars_guess(image: np.ndarray) -> photutils.psf.EPSFStars:
    # background_rms = MADStdBackgroundRMS(sigma_clip=SigmaClip(3))(image)
    mean, median, std = sigma_clipped_stats(image, sigma=clip_sigma)
    threshold = median + (threshold_factor * std)

    # The idea here is to run a "greedy" starfinder that finds a lot more candidates than we need and then
    # to filter out the bright and isolated stars
    peaks_tbl = DAOStarFinder(threshold, fwhm_guess)(image)
    peaks_tbl.rename_columns(['xcentroid', 'ycentroid'], ['x', 'y'])

    peaks_tbl = cut_edges(peaks_tbl, cutout_size, image.shape[0])
    # TODO this gets medianed away with the image combine approach, so more star more good?
    # stars_tbl = cut_close_stars(peaks_tbl, cutoff_dist=3)
    stars_tbl = peaks_tbl

    image_no_background = image - median
    stars = extract_stars(NDData(image_no_background), stars_tbl, size=cutout_size)
    return stars


def make_epsf_combine(image: np.ndarray) -> photutils.psf.EPSFModel:
    upsample_factor = 4  # see Jay Anderson, 2016 but for HST so this may not be optimal here...

    stars = make_stars_guess(image)

    avg_center = np.sum([np.array(st.cutout_center) for st in stars], axis=0) / len(stars)

    # upsample_image should scale and shift/resample an image with a FFT, aligning the cutouts more precisely
    combined = np.median([upsample_image(star.data, upsample_factor=upsample_factor,
                                                xshift=star.cutout_center[0] - avg_center[0],
                                                yshift=star.cutout_center[1] - avg_center[1]
                                                ).real
                                 for star in stars], axis=0)

    # TODO What we return here needs to actually use the image in it's __call__ operator to work as a model
    # type: ignore
    return photutils.psf.EPSFModel(combined, flux=None, oversampling=upsample_factor)  # flux=None should force normalization



def make_epsf_fit(image: np.ndarray) -> photutils.psf.EPSFModel:
    # TODO
    #  how does the psf class interpolate/smooth the data internaly? Jay+Anderson2016 says to use a x^4 polynomial
    #  Also evaluating epsf(x,y) shows the high order noise, but if you only evaluate at the sample points it
    #  does look halfway decent so maybe the fit just creates garbage where it's not constrained by smoothing
    stars = make_stars_guess(image)
    x, y = np.meshgrid(np.linspace(-1, 1, 5), np.linspace(-1, 1, 5))
    d = np.sqrt(x * x + y * y)
    sigma, mu = 1.0, 0.0
    gauss_kernel = np.exp(-((d - mu) ** 2 / (2.0 * sigma ** 2)))

    epsf, fitted_stars = EPSFBuilder(oversampling=oversampling,
                                     maxiters=epsfbuilder_iters,
                                     progress_bar=True,
                                     smoothing_kernel=gauss_kernel/np.sum(gauss_kernel))(stars)
                                     #smoothing_kernel='quadratic')(stars)
    return epsf


def do_photometry_epsf(epsf: photutils.psf.EPSFModel, image: np.ndarray) -> Table:

    epsf = photutils.psf.prepare_psf_model(epsf, renormalize_psf=False)  # renormalize is super slow...
    # TODO
    #  Okay, somehow this seems to be the issue: CompoundModel._map_parameters somehow gets screwed up by the way
    #  prepare_psf_model combines models into a tree and you get wrong parameter names (offset_0_1 -> offset_4)
    #  For some reason the call to _map_parameters really messes up the debugger when you try to step in.
    #  Figure out if we can maybe add the missing Parameters ourselves somehow. But working with these models seems
    #  unpleasant as far as just adding parameters
    #  This issue is only triggered if you get multiple stars per group as then the compound of two star models is
    #  constructed

    background_rms = MADStdBackgroundRMS()

    _, img_median, img_stddev = sigma_clipped_stats(image, sigma=clip_sigma)
    threshold = img_median + (threshold_factor * img_stddev)
    fwhm_guess = FWHM_estimate(epsf)
    star_finder = DAOStarFinder(threshold, fwhm_guess)

    grouper = DAOGroup(separation_factor*fwhm_guess)

    shape = (epsf.psfmodel.shape/epsf.psfmodel.oversampling).astype(np.int64)

    epsf.fwhm = astropy.modeling.Parameter('fwhm', 'this is not the way to add this I think')
    epsf.fwhm.value = fwhm_guess
    # photometry = IterativelySubtractedPSFPhotometry(
    #     finder=star_finder,
    #     group_maker=grouper,
    #     bkg_estimator=background_rms,
    #     psf_model=epsf,
    #     fitter=LevMarLSQFitter(),
    #     niters=3,
    #     fitshape=shape
    # )
    photometry = BasicPSFPhotometry(
        finder=star_finder,
        group_maker=grouper,
        bkg_estimator=background_rms,
        psf_model=epsf,
        fitter=LevMarLSQFitter(),
        fitshape=shape
    )


    return photometry(image)


def verify_methods_with_grid(filename='output_files/grid_16.fits'):
    img = fits.open(filename)[0].data

    epsf_fit = make_epsf_fit(img)
    epsf_combine = make_epsf_combine(img)

    table_fit = do_photometry_epsf(epsf_fit, img)
    table_combine = do_photometry_epsf(epsf_combine, img)

    plt.figure()
    plt.title('EPSF from fit')
    plt.imshow(epsf_fit.data+0.01, norm=LogNorm())

    plt.figure()
    plt.title('EPSF from image combination')
    plt.imshow(epsf_combine.data+0.01, norm=LogNorm())

    plt.figure()
    plt.title('EPSF internal fit')
    plt.imshow(img, norm=LogNorm())
    plt.plot(table_fit['x_fit'], table_fit['y_fit'], 'r.', alpha=0.7)

    plt.figure()
    plt.title('EPSF image combine')
    plt.imshow(img, norm=LogNorm())
    plt.plot(table_combine['x_fit'], table_combine['y_fit'], 'r.', alpha=0.7)

    return epsf_fit, epsf_combine, table_fit, table_combine


if __name__ == '__main__':
    from astropy.io import fits

    # Original
    img = fits.open('output_files/grid_15.fits')[0].data
    #epsf = make_epsf_combine(img)
    epsf = make_epsf_fit(img)
    table_psf = do_photometry_epsf(epsf, img)
    # table_basic = do_photometry_basic(img,3)
