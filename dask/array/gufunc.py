from __future__ import absolute_import, division, print_function

import numpy as np
from itertools import count
import re

try:
    from cytoolz import concat, merge, unique
except ImportError:
    from toolz import concat, merge, unique

from .core import Array, asarray, blockwise, getitem, apply_infer_dtype
from .utils import meta_from_array
from ..highlevelgraph import HighLevelGraph
from ..core import flatten


# Modified version of `numpy.lib.function_base._parse_gufunc_signature`
# Modifications:
#   - Allow for zero input arguments
# See https://docs.scipy.org/doc/numpy/reference/c-api.generalized-ufuncs.html
_DIMENSION_NAME = r'\w+'
_CORE_DIMENSION_LIST = '(?:{0:}(?:,{0:})*,?)?'.format(_DIMENSION_NAME)
_ARGUMENT = r'\({}\)'.format(_CORE_DIMENSION_LIST)
_INPUT_ARGUMENTS = '(?:{0:}(?:,{0:})*,?)?'.format(_ARGUMENT)
_OUTPUT_ARGUMENTS = '{0:}(?:,{0:})*'.format(_ARGUMENT)  # Use `'{0:}(?:,{0:})*,?'` if gufunc-
# signature should be allowed for length 1 tuple returns
_SIGNATURE = '^{0:}->{1:}$'.format(_INPUT_ARGUMENTS, _OUTPUT_ARGUMENTS)


def _parse_gufunc_signature(signature):
    """
    Parse string signatures for a generalized universal function.

    Arguments
    ---------
    signature : string
        Generalized universal function signature, e.g., ``(m,n),(n,p)->(m,p)``
        for ``np.matmul``.

    Returns
    -------
    Tuple of input and output core dimensions parsed from the signature, each
    of the form List[Tuple[str, ...]], except for one output. For one  output
    core dimension is not a list, but of the form Tuple[str, ...]
    """
    signature = signature.replace(' ', '')
    if not re.match(_SIGNATURE, signature):
        raise ValueError('Not a valid gufunc signature: {}'.format(signature))
    in_txt, out_txt = signature.split('->')
    ins = [tuple(re.findall(_DIMENSION_NAME, arg))
           for arg in re.findall(_ARGUMENT, in_txt)]
    outs = [tuple(re.findall(_DIMENSION_NAME, arg))
            for arg in re.findall(_ARGUMENT, out_txt)]
    outs = outs[0] if ((len(outs) == 1) and (out_txt[-1] != ',')) else outs
    return ins, outs


def _validate_normalize_axes(axes, axis, keepdims, input_coredimss, output_coredimss):
    """
    Validates logic of `axes`/`axis`/`keepdims` arguments and normalize them.
    Refer to [1]_ for details

    Arguments
    ---------
    axes: List of tuples
    axis: int
    keepdims: bool
    input_coredimss: List of Tuple of dims
    output_coredimss: List of Tuple of dims

    Returns
    -------
    input_axes: List of tuple of int
    output_axes: List of tuple of int

    References
    ----------
    .. [1] https://docs.scipy.org/doc/numpy/reference/ufuncs.html#optional-keyword-arguments
    """
    nin = len(input_coredimss)
    nout = 1 if not isinstance(output_coredimss, list) else len(output_coredimss)

    if axes is not None and axis is not None:
        raise ValueError("Only one of `axis` or `axes` keyword arguments should be given")
    if axes and not isinstance(axes, list):
        raise ValueError("`axes` has to be of type list")

    output_coredimss = output_coredimss if nout > 1 else [output_coredimss]
    filtered_core_dims = list(filter(len, input_coredimss))
    nr_outputs_with_coredims = len([True for x in output_coredimss if len(x) > 0])

    if keepdims:
        if nr_outputs_with_coredims > 0:
            raise ValueError("`keepdims` can only be used for scalar outputs")
        output_coredimss = len(output_coredimss) * [filtered_core_dims[0]]

    core_dims = input_coredimss + output_coredimss
    if axis is not None:
        if not isinstance(axis, int):
            raise ValueError("`axis` argument has to be an integer value")
        if filtered_core_dims:
            cd0 = filtered_core_dims[0]
            if len(cd0) != 1:
                raise ValueError("`axis` can be used only, if one core dimension is present")
            for cd in filtered_core_dims:
                if cd0 != cd:
                    raise ValueError("To use `axis`, all core dimensions have to be equal")

    # Expand dafaults or axis
    if axes is None:
        if axis is not None:
            axes = [(axis,) if cd else tuple() for cd in core_dims]
        else:
            axes = [tuple(range(-len(icd), 0)) for icd in core_dims]
    elif not isinstance(axes, list):
        raise ValueError("`axes` argument has to be a list")
    axes = [(a,) if isinstance(a, int) else a for a in axes]

    if (((nr_outputs_with_coredims == 0) and (nin != len(axes)) and (nin + nout != len(axes))) or
            ((nr_outputs_with_coredims > 0) and (nin + nout != len(axes)))):
        raise ValueError("The number of `axes` entries is not equal the number of input and output arguments")

    # Treat outputs
    output_axes = axes[nin:]
    output_axes = output_axes if output_axes else [tuple(range(-len(ocd), 0)) for ocd in output_coredimss]
    input_axes = axes[:nin]

    # Assert we have as many axes as output core dimensions
    for idx, (iax, icd) in enumerate(zip(input_axes, input_coredimss)):
        if len(iax) != len(icd):
            raise ValueError("The number of `axes` entries for argument #{} is not equal "
                             "the number of respective input core dimensions in signature"
                             .format(idx))
    if not keepdims:
        for idx, (oax, ocd) in enumerate(zip(output_axes, output_coredimss)):
            if len(oax) != len(ocd):
                raise ValueError("The number of `axes` entries for argument #{} is not equal "
                                 "the number of respective output core dimensions in signature"
                                 .format(idx))
    else:
        if input_coredimss:
            icd0 = input_coredimss[0]
            for icd in input_coredimss:
                if icd0 != icd:
                    raise ValueError("To use `keepdims`, all core dimensions have to be equal")
            iax0 = input_axes[0]
            output_axes = [iax0 for _ in output_coredimss]

    return input_axes, output_axes


def apply_gufunc(func, signature, *args, **kwargs):
    """
    Apply a generalized ufunc or similar python function to arrays.

    ``signature`` determines if the function consumes or produces core
    dimensions. The remaining dimensions in given input arrays (``*args``)
    are considered loop dimensions and are required to broadcast
    naturally against each other.

    In other terms, this function is like np.vectorize, but for
    the blocks of dask arrays. If the function itself shall also
    be vectorized use ``vectorize=True`` for convenience.

    Parameters
    ----------
    func : callable
        Function to call like ``func(*args, **kwargs)`` on input arrays
        (``*args``) that returns an array or tuple of arrays. If multiple
        arguments with non-matching dimensions are supplied, this function is
        expected to vectorize (broadcast) over axes of positional arguments in
        the style of NumPy universal functions [1]_ (if this is not the case,
        set ``vectorize=True``). If this function returns multiple outputs,
        ``output_core_dims`` has to be set as well.
    signature: string
        Specifies what core dimensions are consumed and produced by ``func``.
        According to the specification of numpy.gufunc signature [2]_
    *args : numeric
        Input arrays or scalars to the callable function.
    axes: List of tuples, optional, keyword only
        A list of tuples with indices of axes a generalized ufunc should operate on.
        For instance, for a signature of ``"(i,j),(j,k)->(i,k)"`` appropriate for
        matrix multiplication, the base elements are two-dimensional matrices
        and these are taken to be stored in the two last axes of each argument. The
        corresponding axes keyword would be ``[(-2, -1), (-2, -1), (-2, -1)]``.
        For simplicity, for generalized ufuncs that operate on 1-dimensional arrays
        (vectors), a single integer is accepted instead of a single-element tuple,
        and for generalized ufuncs for which all outputs are scalars, the output
        tuples can be omitted.
    axis: int, optional, keyword only
        A single axis over which a generalized ufunc should operate. This is a short-cut
        for ufuncs that operate over a single, shared core dimension, equivalent to passing
        in axes with entries of (axis,) for each single-core-dimension argument and ``()`` for
        all others. For instance, for a signature ``"(i),(i)->()"``, it is equivalent to passing
        in ``axes=[(axis,), (axis,), ()]``.
    keepdims: bool, optional, keyword only
        If this is set to True, axes which are reduced over will be left in the result as
        a dimension with size one, so that the result will broadcast correctly against the
        inputs. This option can only be used for generalized ufuncs that operate on inputs
        that all have the same number of core dimensions and with outputs that have no core
        dimensions , i.e., with signatures like ``"(i),(i)->()"`` or ``"(m,m)->()"``.
        If used, the location of the dimensions in the output can be controlled with axes
        and axis.
    output_dtypes : Optional, dtype or list of dtypes, keyword only
        Valid numpy dtype specification or list thereof.
        If not given, a call of ``func`` with a small set of data
        is performed in order to try to  automatically determine the
        output dtypes.
    output_sizes : dict, optional, keyword only
        Optional mapping from dimension names to sizes for outputs. Only used if
        new core dimensions (not found on inputs) appear on outputs.
    vectorize: bool, keyword only
        If set to ``True``, ``np.vectorize`` is applied to ``func`` for
        convenience. Defaults to ``False``.
    allow_rechunk: Optional, bool, keyword only
        Allows rechunking, otherwise chunk sizes need to match and core
        dimensions are to consist only of one chunk.
        Warning: enabling this can increase memory usage significantly.
        Defaults to ``False``.
    **kwargs : dict
        Extra keyword arguments to pass to `func`

    Returns
    -------
    Single dask.array.Array or tuple of dask.array.Array

    Examples
    --------
    >>> import dask.array as da
    >>> import numpy as np
    >>> def stats(x):
    ...     return np.mean(x, axis=-1), np.std(x, axis=-1)
    >>> a = da.random.normal(size=(10,20,30), chunks=(5, 10, 30))
    >>> mean, std = da.apply_gufunc(stats, "(i)->(),()", a)
    >>> mean.compute().shape
    (10, 20)


    >>> def outer_product(x, y):
    ...     return np.einsum("i,j->ij", x, y)
    >>> a = da.random.normal(size=(   20,30), chunks=(10, 30))
    >>> b = da.random.normal(size=(10, 1,40), chunks=(5, 1, 40))
    >>> c = da.apply_gufunc(outer_product, "(i),(j)->(i,j)", a, b, vectorize=True)
    >>> c.compute().shape
    (10, 20, 30, 40)

    References
    ----------
    .. [1] https://docs.scipy.org/doc/numpy/reference/ufuncs.html
    .. [2] https://docs.scipy.org/doc/numpy/reference/c-api.generalized-ufuncs.html
    """
    axes = kwargs.pop("axes", None)
    axis = kwargs.pop("axis", None)
    keepdims = kwargs.pop("keepdims", False)
    output_dtypes = kwargs.pop("output_dtypes", None)
    output_sizes = kwargs.pop("output_sizes", None)
    vectorize = kwargs.pop("vectorize", None)
    allow_rechunk = kwargs.pop("allow_rechunk", False)

    # Input processing:
    ## Signature
    if not isinstance(signature, str):
        raise TypeError('`signature` has to be of type string')
    input_coredimss, output_coredimss = _parse_gufunc_signature(signature)

    ## Determine nout: nout = None for functions of one direct return; nout = int for return tuples
    nout = None if not isinstance(output_coredimss, list) else len(output_coredimss)

    ## Determine and handle output_dtypes
    if output_dtypes is None:
        if vectorize:
            tempfunc = np.vectorize(func, signature=signature)
        else:
            tempfunc = func
        output_dtypes = apply_infer_dtype(tempfunc, args, kwargs, "apply_gufunc", "output_dtypes", nout)

    if isinstance(output_dtypes, (tuple, list)):
        if nout is None:
            if len(output_dtypes) > 1:
                raise ValueError(("Must specify single dtype or list of one dtype "
                                  "for `output_dtypes` for function with one output"))
            otypes = output_dtypes
            output_dtypes = output_dtypes[0]
        else:
            otypes = output_dtypes
    else:
        if nout is not None:
            raise ValueError("Must specify tuple of dtypes for `output_dtypes` for function with multiple outputs")
        otypes = [output_dtypes]

    ## Vectorize function, if required
    if vectorize:
        func = np.vectorize(func, signature=signature, otypes=otypes)

    ## Miscellaneous
    if output_sizes is None:
        output_sizes = {}

    ## Axes
    input_axes, output_axes = _validate_normalize_axes(axes, axis, keepdims, input_coredimss, output_coredimss)

    # Main code:
    ## Cast all input arrays to dask
    args = [asarray(a) for a in args]

    if len(input_coredimss) != len(args):
        ValueError("According to `signature`, `func` requires %d arguments, but %s given"
                   % (len(input_coredimss), len(args)))

    ## Axes: transpose input arguments
    transposed_args = []
    for arg, iax, input_coredims in zip(args, input_axes, input_coredimss):
        shape = arg.shape
        iax = tuple(a if a < 0 else a - len(shape) for a in iax)
        tidc = tuple(i for i in range(-len(shape) + 0, 0) if i not in iax) + iax

        transposed_arg = arg.transpose(tidc)
        transposed_args.append(transposed_arg)
    args = transposed_args

    ## Assess input args for loop dims
    input_shapes = [a.shape for a in args]
    input_chunkss = [a.chunks for a in args]
    num_loopdims = [len(s) - len(cd) for s, cd in zip(input_shapes, input_coredimss)]
    max_loopdims = max(num_loopdims) if num_loopdims else None
    core_input_shapes = [dict(zip(icd, s[n:])) for s, n, icd in zip(input_shapes, num_loopdims, input_coredimss)]
    core_shapes = merge(*core_input_shapes)
    core_shapes.update(output_sizes)

    loop_input_dimss = [tuple("__loopdim%d__" % d for d in range(max_loopdims - n, max_loopdims)) for n in num_loopdims]
    input_dimss = [l + c for l, c in zip(loop_input_dimss, input_coredimss)]

    loop_output_dims = max(loop_input_dimss, key=len) if loop_input_dimss else tuple()

    ## Assess input args for same size and chunk sizes
    ### Collect sizes and chunksizes of all dims in all arrays
    dimsizess = {}
    chunksizess = {}
    for dims, shape, chunksizes in zip(input_dimss, input_shapes, input_chunkss):
        for dim, size, chunksize in zip(dims, shape, chunksizes):
            dimsizes = dimsizess.get(dim, [])
            dimsizes.append(size)
            dimsizess[dim] = dimsizes
            chunksizes_ = chunksizess.get(dim, [])
            chunksizes_.append(chunksize)
            chunksizess[dim] = chunksizes_
    ### Assert correct partitioning, for case:
    for dim, sizes in dimsizess.items():
        #### Check that the arrays have same length for same dimensions or dimension `1`
        if set(sizes).union({1}) != {1, max(sizes)}:
            raise ValueError("Dimension `'{}'` with different lengths in arrays".format(dim))
        if not allow_rechunk:
            chunksizes = chunksizess[dim]
            #### Check if core dimensions consist of only one chunk
            if (dim in core_shapes) and (chunksizes[0][0] < core_shapes[dim]):
                raise ValueError("Core dimension `'{}'` consists of multiple chunks. To fix, rechunk into a single \
chunk along this dimension or set `allow_rechunk=True`, but beware that this may increase memory usage \
significantly.".format(dim))
            #### Check if loop dimensions consist of same chunksizes, when they have sizes > 1
            relevant_chunksizes = list(unique(c for s, c in zip(sizes, chunksizes) if s > 1))
            if len(relevant_chunksizes) > 1:
                raise ValueError("Dimension `'{}'` with different chunksize present".format(dim))

    ## Apply function - use blockwise here
    arginds = list(concat(zip(args, input_dimss)))

    ### Use existing `blockwise` but only with loopdims to enforce
    ### concatenation for coredims that appear also at the output
    ### Modifying `blockwise` could improve things here.
    tmp = blockwise(
        func,
        loop_output_dims,
        *arginds,
        dtype=int,  # Only dummy dtype, anyone will do
        concatenate=True,
        **kwargs
    )

    ## Prepare output shapes
    loop_output_shape = tmp.shape
    loop_output_chunks = tmp.chunks
    keys = list(flatten(tmp.__dask_keys__()))
    name, token = keys[0][0].split('-')

    ### *) Treat direct output
    if nout is None:
        output_coredimss = [output_coredimss]
        output_dtypes = [output_dtypes]

    ## Split output
    leaf_arrs = []
    for i, ocd, odt, oax in zip(count(0), output_coredimss, output_dtypes, output_axes):
        core_output_shape = tuple(core_shapes[d] for d in ocd)
        core_chunkinds = len(ocd) * (0,)
        output_shape = loop_output_shape + core_output_shape
        output_chunks = loop_output_chunks + core_output_shape
        leaf_name = "%s_%d-%s" % (name, i, token)
        leaf_dsk = {(leaf_name,) + key[1:] + core_chunkinds: ((getitem, key, i) if nout else key) for key in keys}
        graph = HighLevelGraph.from_collections(leaf_name, leaf_dsk, dependencies=[tmp])
        meta = meta_from_array(tmp._meta, len(output_shape), dtype=odt)
        leaf_arr = Array(graph,
                         leaf_name,
                         chunks=output_chunks,
                         shape=output_shape,
                         meta=meta)

        ### Axes:
        if keepdims:
            slices = len(leaf_arr.shape) * (slice(None),) + len(oax) * (np.newaxis,)
            leaf_arr = leaf_arr[slices]

        tidcs = [None] * len(leaf_arr.shape)
        for i, oa in zip(range(-len(oax), 0), oax):
            tidcs[oa] = i
        j = 0
        for i in range(len(tidcs)):
            if tidcs[i] is None:
                tidcs[i] = j
                j += 1
        leaf_arr = leaf_arr.transpose(tidcs)
        leaf_arrs.append(leaf_arr)

    return leaf_arrs if nout else leaf_arrs[0]  # Undo *) from above


class gufunc(object):
    """
    Binds `pyfunc` into ``dask.array.apply_gufunc`` when called.

    Parameters
    ----------
    pyfunc : callable
        Function to call like ``func(*args, **kwargs)`` on input arrays
        (``*args``) that returns an array or tuple of arrays. If multiple
        arguments with non-matching dimensions are supplied, this function is
        expected to vectorize (broadcast) over axes of positional arguments in
        the style of NumPy universal functions [1]_ (if this is not the case,
        set ``vectorize=True``). If this function returns multiple outputs,
        ``output_core_dims`` has to be set as well.
    signature : String, keyword only
        Specifies what core dimensions are consumed and produced by ``func``.
        According to the specification of numpy.gufunc signature [2]_
    axes: List of tuples, optional, keyword only
        A list of tuples with indices of axes a generalized ufunc should operate on.
        For instance, for a signature of ``"(i,j),(j,k)->(i,k)"`` appropriate for
        matrix multiplication, the base elements are two-dimensional matrices
        and these are taken to be stored in the two last axes of each argument. The
        corresponding axes keyword would be ``[(-2, -1), (-2, -1), (-2, -1)]``.
        For simplicity, for generalized ufuncs that operate on 1-dimensional arrays
        (vectors), a single integer is accepted instead of a single-element tuple,
        and for generalized ufuncs for which all outputs are scalars, the output
        tuples can be omitted.
    axis: int, optional, keyword only
        A single axis over which a generalized ufunc should operate. This is a short-cut
        for ufuncs that operate over a single, shared core dimension, equivalent to passing
        in axes with entries of (axis,) for each single-core-dimension argument and ``()`` for
        all others. For instance, for a signature ``"(i),(i)->()"``, it is equivalent to passing
        in ``axes=[(axis,), (axis,), ()]``.
    keepdims: bool, optional, keyword only
        If this is set to True, axes which are reduced over will be left in the result as
        a dimension with size one, so that the result will broadcast correctly against the
        inputs. This option can only be used for generalized ufuncs that operate on inputs
        that all have the same number of core dimensions and with outputs that have no core
        dimensions , i.e., with signatures like ``"(i),(i)->()"`` or ``"(m,m)->()"``.
        If used, the location of the dimensions in the output can be controlled with axes
        and axis.
    output_dtypes : Optional, dtype or list of dtypes, keyword only
        Valid numpy dtype specification or list thereof.
        If not given, a call of ``func`` with a small set of data
        is performed in order to try to  automatically determine the
        output dtypes.
    output_sizes : dict, optional, keyword only
        Optional mapping from dimension names to sizes for outputs. Only used if
        new core dimensions (not found on inputs) appear on outputs.
    vectorize: bool, keyword only
        If set to ``True``, ``np.vectorize`` is applied to ``func`` for
        convenience. Defaults to ``False``.
    allow_rechunk: Optional, bool, keyword only
        Allows rechunking, otherwise chunk sizes need to match and core
        dimensions are to consist only of one chunk.
        Warning: enabling this can increase memory usage significantly.
        Defaults to ``False``.

    Returns
    -------
    Wrapped function

    Examples
    --------
    >>> import dask.array as da
    >>> import numpy as np
    >>> a = da.random.normal(size=(10,20,30), chunks=(5, 10, 30))
    >>> def stats(x):
    ...     return np.mean(x, axis=-1), np.std(x, axis=-1)
    >>> gustats = da.gufunc(stats, signature="(i)->(),()", output_dtypes=(float, float))
    >>> mean, std = gustats(a)
    >>> mean.compute().shape
    (10, 20)


    >>> a = da.random.normal(size=(   20,30), chunks=(10, 30))
    >>> b = da.random.normal(size=(10, 1,40), chunks=(5, 1, 40))
    >>> def outer_product(x, y):
    ...     return np.einsum("i,j->ij", x, y)
    >>> guouter_product = da.gufunc(outer_product, signature="(i),(j)->(i,j)", output_dtypes=float, vectorize=True)
    >>> c = guouter_product(a, b)
    >>> c.compute().shape
    (10, 20, 30, 40)

    References
    ----------
    .. [1] https://docs.scipy.org/doc/numpy/reference/ufuncs.html
    .. [2] https://docs.scipy.org/doc/numpy/reference/c-api.generalized-ufuncs.html
    """
    def __init__(self, pyfunc, **kwargs):
        self.pyfunc = pyfunc
        self.signature = kwargs.pop("signature", None)
        self.vectorize = kwargs.pop("vectorize", False)
        self.axes = kwargs.pop("axes", None)
        self.axis = kwargs.pop("axis", None)
        self.keepdims = kwargs.pop("keepdims", False)
        self.output_sizes = kwargs.pop("output_sizes", None)
        self.output_dtypes = kwargs.pop("output_dtypes", None)
        self.allow_rechunk = kwargs.pop("allow_rechunk", False)
        if kwargs:
            raise TypeError("Unsupported keyword argument(s) provided")

        self.__doc__ = """
        Bound ``dask.array.gufunc``
        func: ``{func}``
        signature: ``'{signature}'``

        Parameters
        ----------
        *args : numpy/dask arrays or scalars
            Arrays to which to apply to ``func``. Core dimensions as specified in
            ``signature`` need to come last.
        **kwargs : dict
            Extra keyword arguments to pass to ``func``

        Returns
        -------
        Single dask.array.Array or tuple of dask.array.Array
        """.format(func=str(self.pyfunc), signature=self.signature)

    def __call__(self, *args, **kwargs):
        return apply_gufunc(self.pyfunc,
                            self.signature,
                            *args,
                            vectorize=self.vectorize,
                            axes=self.axes,
                            axis=self.axis,
                            keepdims=self.keepdims,
                            output_sizes=self.output_sizes,
                            output_dtypes=self.output_dtypes,
                            allow_rechunk=self.allow_rechunk or kwargs.pop("allow_rechunk", False),
                            **kwargs)


def as_gufunc(signature=None, **kwargs):
    """
    Decorator for ``dask.array.gufunc``.

    Parameters
    ----------
    signature : String
        Specifies what core dimensions are consumed and produced by ``func``.
        According to the specification of numpy.gufunc signature [2]_
    axes: List of tuples, optional, keyword only
        A list of tuples with indices of axes a generalized ufunc should operate on.
        For instance, for a signature of ``"(i,j),(j,k)->(i,k)"`` appropriate for
        matrix multiplication, the base elements are two-dimensional matrices
        and these are taken to be stored in the two last axes of each argument. The
        corresponding axes keyword would be ``[(-2, -1), (-2, -1), (-2, -1)]``.
        For simplicity, for generalized ufuncs that operate on 1-dimensional arrays
        (vectors), a single integer is accepted instead of a single-element tuple,
        and for generalized ufuncs for which all outputs are scalars, the output
        tuples can be omitted.
    axis: int, optional, keyword only
        A single axis over which a generalized ufunc should operate. This is a short-cut
        for ufuncs that operate over a single, shared core dimension, equivalent to passing
        in axes with entries of (axis,) for each single-core-dimension argument and ``()`` for
        all others. For instance, for a signature ``"(i),(i)->()"``, it is equivalent to passing
        in ``axes=[(axis,), (axis,), ()]``.
    keepdims: bool, optional, keyword only
        If this is set to True, axes which are reduced over will be left in the result as
        a dimension with size one, so that the result will broadcast correctly against the
        inputs. This option can only be used for generalized ufuncs that operate on inputs
        that all have the same number of core dimensions and with outputs that have no core
        dimensions , i.e., with signatures like ``"(i),(i)->()"`` or ``"(m,m)->()"``.
        If used, the location of the dimensions in the output can be controlled with axes
        and axis.
    output_dtypes : Optional, dtype or list of dtypes, keyword only
        Valid numpy dtype specification or list thereof.
        If not given, a call of ``func`` with a small set of data
        is performed in order to try to  automatically determine the
        output dtypes.
    output_sizes : dict, optional, keyword only
        Optional mapping from dimension names to sizes for outputs. Only used if
        new core dimensions (not found on inputs) appear on outputs.
    vectorize: bool, keyword only
        If set to ``True``, ``np.vectorize`` is applied to ``func`` for
        convenience. Defaults to ``False``.
    allow_rechunk: Optional, bool, keyword only
        Allows rechunking, otherwise chunk sizes need to match and core
        dimensions are to consist only of one chunk.
        Warning: enabling this can increase memory usage significantly.
        Defaults to ``False``.

    Returns
    -------
    Decorator for `pyfunc` that itself returns a `gufunc`.

    Examples
    --------
    >>> import dask.array as da
    >>> import numpy as np
    >>> a = da.random.normal(size=(10,20,30), chunks=(5, 10, 30))
    >>> @da.as_gufunc("(i)->(),()", output_dtypes=(float, float))
    ... def stats(x):
    ...     return np.mean(x, axis=-1), np.std(x, axis=-1)
    >>> mean, std = stats(a)
    >>> mean.compute().shape
    (10, 20)

    >>> a = da.random.normal(size=(   20,30), chunks=(10, 30))
    >>> b = da.random.normal(size=(10, 1,40), chunks=(5, 1, 40))
    >>> @da.as_gufunc("(i),(j)->(i,j)", output_dtypes=float, vectorize=True)
    ... def outer_product(x, y):
    ...     return np.einsum("i,j->ij", x, y)
    >>> c = outer_product(a, b)
    >>> c.compute().shape
    (10, 20, 30, 40)

    References
    ----------
    .. [1] https://docs.scipy.org/doc/numpy/reference/ufuncs.html
    .. [2] https://docs.scipy.org/doc/numpy/reference/c-api.generalized-ufuncs.html
    """
    _allowedkeys = {"vectorize", "axes", "axis", "keepdims", "output_sizes", "output_dtypes", "allow_rechunk"}
    if set(_allowedkeys).issubset(kwargs.keys()):
        raise TypeError("Unsupported keyword argument(s) provided")

    def _as_gufunc(pyfunc):
        return gufunc(pyfunc, signature=signature, **kwargs)
    _as_gufunc.__doc__ = """
        Decorator to make ``dask.array.gufunc``
        signature: ``'{signature}'``

        Parameters
        ----------
        pyfunc : callable
            Function matching signature ``'{signature}'``.

        Returns
        -------
        ``dask.array.gufunc``
        """.format(signature=signature)
    return _as_gufunc
