"""
    MatrixDFT: Matrix-based discrete Fourier transforms for computing PSFs.

    See Soummer et al. 2007 JOSA

    The main user interface in this module is a class MatrixFourierTransform.
    Internally this will call one of several subfunctions depending on the
    specified centering type. These have to do with where the (0, 0) element of
    the Fourier transform is located, i.e. where the PSF center ends up.

        - 'FFTSTYLE' centered on one pixel
        - 'SYMMETRIC' centered on crosshairs between middle pixel
        - 'ADJUSTABLE', always centered in output array depending on
          whether it is even or odd

    'ADJUSTABLE' is the default.

    This module was originally called "Slow Fourier Transform", and this
    terminology still appears in some places in the code.  Note that this is
    'slow' only in the sense that if you perform the exact same calculation as
    an FFT, the FFT algorithm is much faster. However this algorithm gives you
    much more flexibility in choosing array sizes and sampling, and often lets
    you replace "fast calculations on very large arrays" with "relatively slow
    calculations on much smaller ones".

    Sign Conventions
    ----------------
    With the sign conventions adopted in poppy (increased optical path length causes the wavefront to lag, and
    corresponds to a negative wavefront error), it is the case that we want a positive sign in the
    exponential for the forward transformation (like the sign of an inverse FFT in numpy convention), and
    a negative sign in the exponential for the backward transformation (like the sign of the forward FFT
    in numpy convention). See the Sign Conventions notebook for further discussion.


    Example
    -------
    mf = matrixDFT.MatrixFourierTransform()
    result = mf.perform(pupilArray, focalplane_size, focalplane_npix)

    History
    -------
    Code originally by A. Sivaramakrishnan
    2010-11-05 Revised normalizations for flux conservation consistent
        with Soummer et al. 2007. Updated documentation.  -- M. Perrin
    2011-2012: Various enhancements, detailed history not kept, sorry.
    2012-05-18: module renamed SFT.py -> matrixDFT.py
    2012-09-26: minor big fixes
    2015-01-21: Eliminate redundant code paths, correct parity flip,
                PEP8 formatting pass (except variable names)-- J. Long
    2020-12-01: Sign convention change, for disambiguation and consistency
                with other optical modeling packages including JWST WAS. - M. Perrin

"""

__all__ = ['MatrixFourierTransform']

import numpy as np
from . import conf
from . import accel_math

accel_math.update_math_settings()
global _ncp
from .accel_math import _ncp

if accel_math._USE_NUMEXPR:
    import numexpr as ne
    
# if accel_math._USE_CUPY:
#     import cupy as np
#     rnd = numpy.round
# else:
#     import numpy as np
#     rnd = np.round

import logging
_log = logging.getLogger('poppy')

FFTSTYLE = 'FFTSTYLE'
FFTRECT = 'FFTRECT'
SYMMETRIC = 'SYMMETRIC'
ADJUSTABLE = 'ADJUSTABLE'
CENTERING_CHOICES = (FFTSTYLE, SYMMETRIC, ADJUSTABLE, FFTRECT)

def matrix_dft(plane, nlamD, npix,
               offset=None, inverse=False, centering=FFTSTYLE):
    """Perform a matrix discrete Fourier transform with selectable
    output sampling and centering.

    Where parameters can be supplied as either scalars or 2-tuples, the first
    element of the 2-tuple is used for the Y dimension and the second for the
    X dimension. This ordering matches that of numpy.ndarray.shape attributes
    and that of Python indexing.

    To achieve exact correspondence to the FFT set nlamD and npix to the size
    of the input array in pixels and use 'FFTSTYLE' centering. (n.b. When
    using `numpy.fft.fft2` you must `numpy.fft.fftshift` the input pupil both
    before and after applying fft2 or else it will introduce a checkerboard
    pattern in the signs of alternating pixels!)

    Parameters
    ----------
    plane : 2D ndarray
        2D array (either real or complex) representing the input image plane or
        pupil plane to transform.
    nlamD : float or 2-tuple of floats (nlamDY, nlamDX)
        Size of desired output region in lambda / D units, assuming that the
        pupil fills the input array (corresponds to 'm' in
        Soummer et al. 2007 4.2). This is in units of the spatial frequency that
        is just Nyquist sampled by the input array.) If given as a tuple,
        interpreted as (nlamDY, nlamDX).
    npix : int or 2-tuple of ints (npixY, npixX)
        Number of pixels per side side of destination plane array (corresponds
        to 'N_B' in Soummer et al. 2007 4.2). This will be the # of pixels in
        the image plane for a forward transformation, in the pupil plane for an
        inverse. If given as a tuple, interpreted as (npixY, npixX).
    inverse : bool, optional
        Is this a forward or inverse transformation? (Default is False,
        implying a forward transformation.)
    centering : {'FFTSTYLE', 'SYMMETRIC', 'ADJUSTABLE'}, optional
        What type of centering convention should be used for this FFT?

        * ADJUSTABLE (the default) For an output array with ODD size n,
          the PSF center will be at the center of pixel (n-1)/2. For an output
          array with EVEN size n, the PSF center will be in the corner between
          pixel (n/2-1, n/2-1) and (n/2, n/2)
        * FFTSTYLE puts the zero-order term in a single pixel.
        * SYMMETRIC spreads the zero-order term evenly between the center
          four pixels

    offset : 2-tuple of floats (offsetY, offsetX)
        For ADJUSTABLE-style transforms, an offset in pixels by which the PSF
        will be displaced from the central pixel (or cross). Given as
        (offsetY, offsetX).
    """
    
    accel_math.update_math_settings()
    global _ncp
    from .accel_math import _ncp
    
    if accel_math._USE_NUMEXPR and not accel_math._USE_CUPY:
        return matrix_dft_numexpr(plane, nlamD, npix,
                                  offset=offset, inverse=inverse, centering=centering)
    float = accel_math._float()

    npupY, npupX = plane.shape

    try:
        if np.isscalar(npix):
            npixY, npixX = float(npix), float(npix)
        else:
            npixY, npixX = tuple(_ncp.asarray(npix, dtype=float))
    except ValueError:
        raise ValueError(
            "'npix' must be supplied as a scalar (for square arrays) or as "
            "a 2-tuple of ints (npixY, npixX)"
        )

    # make sure these are integer values
    if npixX != int(npixX) or npixY != int(npixY):
        raise TypeError("'npix' must be supplied as integer value(s)")

    try:
        if np.isscalar(nlamD):
            nlamDY, nlamDX = float(nlamD), float(nlamD)
        else:
            nlamDY, nlamDX = tuple(_ncp.asarray(nlamD, dtype=float))
    except ValueError:
        raise ValueError(
            "'nlamD' must be supplied as a scalar (for square arrays) or as"
            " a 2-tuple of floats (nlamDY, nlamDX)"
        )

    centering = centering.upper()


    # In the following: X and Y are coordinates in the input plane
    #                   U and V are coordinates in the output plane

    if inverse:
        dX = nlamDX / float(npupX)
        dY = nlamDY / float(npupY)
        dU = 1.0 / float(npixX)
        dV = 1.0 / float(npixY)
    else:
        dU = nlamDX / float(npixX)
        dV = nlamDY / float(npixY)
        dX = 1.0 / float(npupX)
        dY = 1.0 / float(npupY)


    if centering == FFTSTYLE:
        Xs = (_ncp.arange(npupX, dtype=float) - (npupX / 2)) * dX
        Ys = (_ncp.arange(npupY, dtype=float) - (npupY / 2)) * dY

        Us = (_ncp.arange(npixX, dtype=float) - npixX / 2) * dU
        Vs = (_ncp.arange(npixY, dtype=float) - npixY / 2) * dV
    elif centering == ADJUSTABLE:
        if offset is None:
            offsetY, offsetX = 0.0, 0.0
        else:
            try:
                offsetY, offsetX = tuple(_ncp.asarray(offset, dtype=float))
            except ValueError:
                raise ValueError(
                    "'offset' must be supplied as a 2-tuple with "
                    "(y_offset, x_offset) as floating point values"
                )
        Xs = (_ncp.arange(npupX, dtype=float) - float(npupX) / 2.0 - offsetX + 0.5) * dX
        Ys = (_ncp.arange(npupY, dtype=float) - float(npupY) / 2.0 - offsetY + 0.5) * dY

        Us = (_ncp.arange(npixX, dtype=float) - float(npixX) / 2.0 - offsetX + 0.5) * dU
        Vs = (_ncp.arange(npixY, dtype=float) - float(npixY) / 2.0 - offsetY + 0.5) * dV
    elif centering == SYMMETRIC:
        Xs = (_ncp.arange(npupX, dtype=float) - float(npupX) / 2.0 + 0.5) * dX
        Ys = (_ncp.arange(npupY, dtype=float) - float(npupY) / 2.0 + 0.5) * dY

        Us = (_ncp.arange(npixX, dtype=float) - float(npixX) / 2.0 + 0.5) * dU
        Vs = (_ncp.arange(npixY, dtype=float) - float(npixY) / 2.0 + 0.5) * dV
    else:
        raise ValueError("Invalid centering style")

    XU = _ncp.outer(Xs, Us)
    YV = _ncp.outer(Ys, Vs)

    # SIGN CONVENTION: plus signs in exponent for basic forward propagation, with
    # phase increasing with time. This convention differs from prior poppy version < 1.0
    if inverse:
        expYV = _ncp.exp(-2.0 * np.pi * 1j * YV).T
        expXU = _ncp.exp(-2.0 * np.pi * 1j * XU)
        t1 = _ncp.dot(expYV, plane)
        t2 = _ncp.dot(t1, expXU)
    else:
        expXU = _ncp.exp(-2.0 * np.pi * -1j * XU)
        expYV = _ncp.exp(-2.0 * np.pi * -1j * YV).T
        t1 = _ncp.dot(expYV, plane)
        t2 = _ncp.dot(t1, expXU)

    norm_coeff = np.sqrt((nlamDY * nlamDX) / (npupY * npupX * npixY * npixX))
    return norm_coeff * t2


def matrix_dft_numexpr(plane, nlamD, npix,
               offset=None, inverse=False, centering=FFTSTYLE):
    """Perform a matrix discrete Fourier transform with selectable
    output sampling and centering. This version accelerated with numexpr.

    Where parameters can be supplied as either scalars or 2-tuples, the first
    element of the 2-tuple is used for the Y dimension and the second for the
    X dimension. This ordering matches that of numpy.ndarray.shape attributes
    and that of Python indexing.

    To achieve exact correspondence to the FFT set nlamD and npix to the size
    of the input array in pixels and use 'FFTSTYLE' centering. (n.b. When
    using `numpy.fft.fft2` you must `numpy.fft.fftshift` the input pupil both
    before and after applying fft2 or else it will introduce a checkerboard
    pattern in the signs of alternating pixels!)

    Parameters
    ----------
    plane : 2D ndarray
        2D array (either real or complex) representing the input image plane or
        pupil plane to transform.
    nlamD : float or 2-tuple of floats (nlamDY, nlamDX)
        Size of desired output region in lambda / D units, assuming that the
        pupil fills the input array (corresponds to 'm' in
        Soummer et al. 2007 4.2). This is in units of the spatial frequency that
        is just Nyquist sampled by the input array.) If given as a tuple,
        interpreted as (nlamDY, nlamDX).
    npix : int or 2-tuple of ints (npixY, npixX)
        Number of pixels per side side of destination plane array (corresponds
        to 'N_B' in Soummer et al. 2007 4.2). This will be the # of pixels in
        the image plane for a forward transformation, in the pupil plane for an
        inverse. If given as a tuple, interpreted as (npixY, npixX).
    inverse : bool, optional
        Is this a forward or inverse transformation? (Default is False,
        implying a forward transformation.)
    centering : {'FFTSTYLE', 'SYMMETRIC', 'ADJUSTABLE'}, optional
        What type of centering convention should be used for this FFT?

        * ADJUSTABLE (the default) For an output array with ODD size n,
          the PSF center will be at the center of pixel (n-1)/2. For an output
          array with EVEN size n, the PSF center will be in the corner between
          pixel (n/2-1, n/2-1) and (n/2, n/2)
        * FFTSTYLE puts the zero-order term in a single pixel.
        * SYMMETRIC spreads the zero-order term evenly between the center
          four pixels

    offset : 2-tuple of floats (offsetY, offsetX)
        For ADJUSTABLE-style transforms, an offset in pixels by which the PSF
        will be displaced from the central pixel (or cross). Given as
        (offsetY, offsetX).
    """

    npupY, npupX = plane.shape
    float = accel_math._float() # shadow builtin float with either np.float32 or np.float64, depending

    try:
        if np.isscalar(npix):
            npixY, npixX = float(npix), float(npix)
        else:
            npixY, npixX = tuple(np.asarray(npix, dtype=float))
    except ValueError:
        raise ValueError(
            "'npix' must be supplied as a scalar (for square arrays) or as "
            "a 2-tuple of ints (npixY, npixX)"
        )

    # make sure these are integer values
    if npixX != int(npixX) or npixY != int(npixY):
        raise TypeError("'npix' must be supplied as integer value(s)")

    try:
        if np.isscalar(nlamD):
            nlamDY, nlamDX = float(nlamD), float(nlamD)
        else:
            nlamDY, nlamDX = tuple(np.asarray(nlamD, dtype=float))
    except ValueError:
        raise ValueError(
            "'nlamD' must be supplied as a scalar (for square arrays) or as"
            " a 2-tuple of floats (nlamDY, nlamDX)"
        )

    centering = centering.upper()


    # In the following: X and Y are coordinates in the input plane
    #                   U and V are coordinates in the output plane

    if inverse:
        dX = nlamDX / float(npupX)
        dY = nlamDY / float(npupY)
        dU = 1.0 / float(npixX)
        dV = 1.0 / float(npixY)
    else:
        dU = nlamDX / float(npixX)
        dV = nlamDY / float(npixY)
        dX = 1.0 / float(npupX)
        dY = 1.0 / float(npupY)


    # Setup arrays since numexpr can't call arange directly
    float = accel_math._float()
    ar_npupX = np.arange(npupX, dtype=float)
    ar_npupY = np.arange(npupY, dtype=float)
    ar_npixX = np.arange(npixX, dtype=float)
    ar_npixY = np.arange(npixY, dtype=float)

    if centering == FFTSTYLE:
        Xs = ne.evaluate("(ar_npupX - (npupX / 2)) * dX")
        Ys = ne.evaluate("(ar_npupY - (npupY / 2)) * dY")

        Us = ne.evaluate("(ar_npixX - npixX / 2) * dU")
        Vs = ne.evaluate("(ar_npixY - npixY / 2) * dV")

    elif centering == ADJUSTABLE:
        if offset is None:
            offsetY, offsetX = 0.0, 0.0
        else:
            try:
                offsetY, offsetX = tuple(np.asarray(offset, dtype=float))
            except ValueError:
                raise ValueError(
                    "'offset' must be supplied as a 2-tuple with "
                    "(y_offset, x_offset) as floating point values"
                )
        Xs = ne.evaluate("(ar_npupX - (npupX) / 2.0 - offsetX + 0.5) * dX")
        Ys = ne.evaluate("(ar_npupY - (npupY) / 2.0 - offsetY + 0.5) * dY")

        Us = ne.evaluate("(ar_npixX - (npixX) / 2.0 - offsetX + 0.5) * dU")
        Vs = ne.evaluate("(ar_npixY - (npixY) / 2.0 - offsetY + 0.5) * dV")

    elif centering == SYMMETRIC:
        Xs = ne.evaluate("(ar_npupX - (npupX) / 2.0 + 0.5) * dX")
        Ys = ne.evaluate("(ar_npupY - (npupY) / 2.0 + 0.5) * dY")

        Us = ne.evaluate("(ar_npixX - (npixX) / 2.0 + 0.5) * dU")
        Vs = ne.evaluate("(ar_npixY - (npixY) / 2.0 + 0.5) * dV")
    else:
        raise ValueError("Invalid centering style")

    XU = np.outer(Xs, Us)
    YV = np.outer(Ys, Vs)

    two_pi_i = 2*np.pi*1j
    # SIGN CONVENTION: plus signs in exponent for basic forward propagation, with
    # phase increasing with time. This convention differs from prior poppy version < 1.0
    if inverse:
        expYV = ne.evaluate("exp(-two_pi_i * YV)").T
        expXU = ne.evaluate("exp(-two_pi_i * XU)")
        t1 = np.dot(expYV, plane)
        t2 = np.dot(t1, expXU)
    else:
        expYV = ne.evaluate("exp(two_pi_i * YV)").T
        expXU = ne.evaluate("exp(two_pi_i * XU)")
        t1 = np.dot(expYV, plane)
        t2 = np.dot(t1, expXU)

    if not conf.double_precision:
        # Work around numexpr bug where exp results must be complex128
        t2 = np.asarray(t2, dtype=np.complex64)

    norm_coeff = np.sqrt((nlamDY * nlamDX) / (npupY * npupX * npixY * npixX))
    return norm_coeff * t2


def matrix_idft(*args, **kwargs):
    kwargs['inverse'] = True
    return matrix_dft(*args, **kwargs)

matrix_idft.__doc__ = matrix_dft.__doc__.replace(
    'Perform a matrix discrete Fourier transform',
    'Perform an inverse matrix discrete Fourier transform'
)

class MatrixFourierTransform:
    """Implements a discrete matrix Fourier transform for optical propagation,
    following the algorithms discussed in Soummer et al. 2007 JOSA 15 24.

    Parameters
    ----------
    centering : {'FFTSTYLE', 'SYMMETRIC', 'ADJUSTABLE'}, optional
        What type of centering convention should be used for this FFT?

        * ADJUSTABLE (the default) For an output array with ODD size n,
          the PSF center will be at the center of pixel (n-1)/2. For an output
          array with EVEN size n, the PSF center will be in the corner between
          pixel (n/2-1, n/2-1) and (n/2, n/2)
        * FFTSTYLE puts the zero-order term in a single pixel.
        * SYMMETRIC spreads the zero-order term evenly between the center
          four pixels
    verbose : bool
        Deprecated. Use poppy.conf.default_logging_level to set DEBUG level
        logging.

    Example
    -------
    >>> mft = MatrixFourierTransform()  # doctest: +SKIP
    >>> result = mft.perform(pupilArray, focalplane_size, focalplane_npix)  # doctest: +SKIP

    History
    -------
    Code by Sivaramakrishnan based on Soummer et al.
    2010-01 Documentation updated by Perrin
    2013-01 'choice' keyword renamed to 'centering' for clarity. 'choice' is
            retained as an option for back compatibility, however it
            is deprecated.
    2015-01-21: Internals updated to use refactored `matrix_dft` function,
                docstrings made consistent with each other -- J. Long
    """

    def __init__(self, centering="ADJUSTABLE", verbose=False):
        
        accel_math.update_math_settings()
        global _ncp
        from .accel_math import _ncp
        
        self.verbose = verbose
        centering = centering.upper()
        if centering == FFTRECT:  # for backwards compatibility
            centering = FFTSTYLE
        if centering not in CENTERING_CHOICES:
            raise ValueError(
                "'centering' must be one of [ADJUSTABLE, SYMMETRIC, FFTSTYLE]"
            )
        self.centering = centering
        _log.debug("MatrixFourierTransform initialized using centering "
                   "type = {0}".format(centering))

    def _validate_args(self, nlamD, npix, offset):
        if self.centering == SYMMETRIC:
            if not np.isscalar(nlamD) or not np.isscalar(npix):
                raise RuntimeError(
                    'The selected centering mode, {}, does not support '
                    'rectangular arrays.'.format(self.centering)
                )
        if self.centering == FFTSTYLE or self.centering == SYMMETRIC:
            if offset is not None:
                raise RuntimeError(
                    'The selected centering mode, {}, does not support '
                    'position offsets.'.format(self.centering)
                )

    def perform(self, pupil, nlamD, npix, offset=None):
        """Forward matrix discrete Fourier Transform

        Parameters
        ----------
        pupil : 2D ndarray
            2D array (either real or complex) representing the input pupil plane
            to transform.
        nlamD : float or 2-tuple of floats (nlamDY, nlamDX)
            Size of desired output region in lambda / D units, assuming that the
            pupil fills the input array (corresponds to 'm' in
            Soummer et al. 2007 4.2). This is in units of the spatial frequency
            that is just Nyquist sampled by the input array.) If given as a
            tuple, interpreted as (nlamDY, nlamDX).
        npix : int or 2-tuple of ints (npixY, npixX)
            Number of pixels per side side of destination plane array
            (corresponds to 'N_B' in Soummer et al. 2007 4.2). This will be the
            # of pixels in the image plane for a forward transformation, in the
            pupil plane for an inverse. If given as a tuple, interpreted as
            (npixY, npixX).
        offset : 2-tuple of floats (offsetY, offsetX)
            For ADJUSTABLE-style transforms, an offset in pixels by which the
            PSF will be displaced from the central pixel (or cross). Given as
            (offsetY, offsetX).

        Returns
        -------
        complex ndarray
            The Fourier transform of the input
        """
        self._validate_args(nlamD, npix, offset)
        _log.debug(
            "Forward MatrixFourierTransform: array shape {}, "
            "centering style {}, "
            "output region size {} in lambda / D units, "
            "output array size {} pixels, "
            "offset {}".format(pupil.shape, self.centering, nlamD, npix, offset)
        )
        return matrix_dft(pupil, nlamD, npix,
                          centering=self.centering, offset=offset)


    def inverse(self, image, nlamD, npix, offset=None):
        """Inverse matrix discrete Fourier Transform

        Parameters
        ----------
        image : 2D ndarray
            2D array (either real or complex) representing the input image plane
            to transform.
        nlamD : float or 2-tuple of floats (nlamDY, nlamDX)
            Size of desired output region in lambda / D units, assuming that the
            pupil fills the input array (corresponds to 'm' in
            Soummer et al. 2007 4.2). This is in units of the spatial frequency
            that is just Nyquist sampled by the input array.) If given as a
            tuple, interpreted as (nlamDY, nlamDX).
        npix : int or 2-tuple of ints (npixY, npixX)
            Number of pixels per side side of destination plane array
            (corresponds to 'N_B' in Soummer et al. 2007 4.2). This will be the
            # of pixels in the image plane for a forward transformation, in the
            pupil plane for an inverse. If given as a tuple, interpreted as
            (npixY, npixX).
        offset : 2-tuple of floats (offsetY, offsetX)
            For ADJUSTABLE-style transforms, an offset in pixels by which the
            PSF will be displaced from the central pixel (or cross). Given as
            (offsetY, offsetX).

        Returns
        -------
        complex ndarray
            The Fourier transform of the input
        """
        self._validate_args(nlamD, npix, offset)
        _log.debug(
            "Inverse MatrixFourierTransform: array shape {}, "
            "centering style {}, "
            "output region size {} in lambda / D units, "
            "output array size {} pixels, "
            "offset {}".format(image.shape, self.centering, nlamD, npix, offset)
        )
        return matrix_idft(image, nlamD, npix,
                           centering=self.centering, offset=offset)
