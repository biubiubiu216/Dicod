"""Convolutional Sparse Coding with DICOD

Author : tommoral <thomas.moreau@inria.fr>
"""

import logging
import numpy as np
from time import time
from mpi4py import MPI

from .utils.csc import cost
from .utils import constants
from .utils.mpi import broadcast_array
from .utils import debug_flags as flags
from .utils.segmentation import Segmentation
from .coordinate_descent import coordinate_descent

from .reusable_workers import get_reusable_workers
from .reusable_workers import send_command_to_reusable_workers


log = logging.getLogger('dicod')

# debug flags

interactive_exec = "xterm"
interactive_args = ["-fa", "Monospace", "-fs", "12", "-e", "ipython", "-i"]


def dicod(X_i, D, reg, z0=None, n_seg='auto', strategy='greedy',
          use_soft_lock=True, n_jobs=1, w_world='auto', hostfile=None,
          tol=1e-5, max_iter=100000, timeout=None, z_positive=False,
          return_ztz=False, freeze_support=False, timing=False,
          random_state=None, verbose=0, debug=False):
    """DICOD for 2D convolutional sparse coding.

    Parameters
    ----------
    X_i : ndarray, shape (n_channels, *sig_shape)
        Image to encode on the dictionary D
    D : ndarray, shape (n_atoms, n_channels, *atom_shape)
        Current dictionary for the sparse coding
    reg : float
        Regularization parameter
    z0 : ndarray, shape (n_atoms, *valid_shape) or None
        Warm start value for z_hat. If None, z_hat is initialized to 0.
    n_seg : int or { 'auto' }
        Number of segments to use for each dimension. If set to 'auto' use
        segments of twice the size of the dictionary.
    strategy : str in { 'greedy' | 'random' }
        Coordinate selection scheme for the coordinate descent. If set to
        'greedy', the coordinate with the largest value for dz_opt is selected.
        If set to 'random, the coordinate is chosen uniformly on the segment.
    use_soft_lock : boolean
        If set to true, use the soft-lock in LGCD.
    n_jobs : int
        Number of workers used to compute the convolutional sparse coding
        solution.
    w_world : int or {'auto'}
        Number of jobs used per row in the splitting grid. This should divide
        n_jobs.
    hostfile : str
        File containing the cluster information. See MPI documentation to have
        the format of this file.
    tol : float
        Tolerance for the minimal update size in this algorithm.
    max_iter : int
        Maximal number of iteration run by this algorithm.
    timeout : int
        Timeout for the algorithm in seconds
    z_positive : boolean
        If set to true, the activations are constrained to be positive.
    return_ztz : boolean
        If True, returns the constants ztz and ztX, used to compute D-updates.
    freeze_support : boolean
        If set to True, only update the coefficient that are non-zero in z0.
    timing : boolean
        If set to True, log the cost and timing information.
    random_state : None or int or RandomState
        current random state to seed the random number generator.
    verbose : int
        Verbosity level of the algorithm.

    Return
    ------
    z_hat : ndarray, shape (n_atoms, *valid_shape)
        Activation associated to X_i for the given dictionary D
    """
    if n_jobs == 1:
        return coordinate_descent(
            X_i, D, reg, z0=z0, n_seg=n_seg, strategy=strategy, tol=tol,
            max_iter=max_iter, timeout=timeout, z_positive=z_positive,
            freeze_support=freeze_support, return_ztz=return_ztz,
            timing=timing, random_state=random_state, verbose=verbose)

    params = dict(
        strategy=strategy, tol=tol, max_iter=max_iter, timeout=timeout,
        n_seg=n_seg, z_positive=z_positive, verbose=verbose, timing=timing,
        debug=debug, random_state=random_state, reg=reg, return_ztz=return_ztz,
        use_soft_lock=use_soft_lock, has_z0=z0 is not None,
        freeze_support=freeze_support
    )
    n_channels, *sig_shape = X_i.shape
    n_atoms, n_channels, *atom_shape = D.shape
    assert D.ndim - 1 == X_i.ndim

    params['valid_shape'] = valid_shape = tuple([
        size_ax - size_atom_ax + 1
        for size_ax, size_atom_ax in zip(sig_shape, atom_shape)
    ])
    overlap = tuple([size_atom_ax - 1 for size_atom_ax in atom_shape])

    if w_world == 'auto':
        params["workers_topology"] = _find_grid_size(n_jobs, sig_shape)
    else:
        assert n_jobs % w_world == 0
        params["workers_topology"] = w_world, n_jobs // w_world

    # compute a segmentation for the image,
    workers_segments = Segmentation(n_seg=params['workers_topology'],
                                    signal_shape=valid_shape,
                                    overlap=overlap)

    # Make sure we are not below twice the size of the dictionary
    worker_valid_shape = workers_segments.get_seg_shape(0, inner=True)
    for size_atom_ax, size_valid_ax in zip(atom_shape, worker_valid_shape):
        if 2 * size_atom_ax - 1 >= size_valid_ax:
            raise ValueError("Using too many cores.")

    comm = _spawn_workers(n_jobs, hostfile)
    _send_task(comm, X_i, D, reg, z0, workers_segments, params)

    comm.Barrier()

    z_hat, ztz, ztX, _log = _recv_result(
        comm, D.shape, valid_shape, workers_segments, return_ztz=return_ztz,
        timing=timing, verbose=verbose)
    comm.Barrier()

    if timing:
        p_obj = reconstruct_pobj(X_i, D, reg, _log, n_jobs=n_jobs,
                                 valid_shape=valid_shape, z0=z0)
    else:
        p_obj = None
    return z_hat, ztz, ztX, p_obj


def reconstruct_pobj(X, D, reg, _log, n_jobs, valid_shape=None, z0=None):
    n_atoms = D.shape[0]
    if z0 is None:
        z_hat = np.zeros((n_atoms, *valid_shape))
    else:
        z_hat = np.copy(z0)

    # Re-order the updates
    _log.sort()
    max_ii = [0] * n_jobs
    for _, ii, rank, *_ in _log:
        max_ii[rank] = max(max_ii[rank], ii)
    max_ii = np.sum(max_ii)

    up_ii = 0
    p_obj = []
    next_cost = 1
    last_ii = [0] * n_jobs
    for i, (t_update, ii, rank, k0, pt0, dz) in enumerate(_log):
        z_hat[k0][tuple(pt0)] += dz
        up_ii += ii - last_ii[rank]
        print("\rReconstructing cost {:7.2%}"
              .format(np.log(up_ii)/np.log(max_ii)), end='', flush=True)
        last_ii[rank] = ii
        if up_ii >= next_cost:
            p_obj.append((up_ii, t_update, cost(X, z_hat, D, reg)))
            next_cost = next_cost * 2
    print('\rReconstruction cost: done'.ljust(40))

    p_obj.append((up_ii, t_update, cost(X, z_hat, D, reg)))
    return np.array(p_obj)


def _find_grid_size(n_jobs, sig_shape):
    if len(sig_shape) == 1:
        return (n_jobs,)
    elif len(sig_shape) == 2:
        height, width = sig_shape
        w_world, h_world = 1, n_jobs
        w_ratio = width * n_jobs / height
        for i in range(2, n_jobs + 1):
            if n_jobs % i != 0:
                continue
            j = n_jobs // i
            ratio = width * j / (height * i)
            if abs(ratio - 1) < abs(w_ratio - 1):
                w_ratio = ratio
                w_world, h_world = i, j
        return w_world, h_world
    else:
        raise NotImplementedError("")


def _spawn_workers(n_jobs, hostfile):
    comm = get_reusable_workers(n_jobs, hostfile=hostfile)
    send_command_to_reusable_workers(constants.TAG_WORKER_RUN_DICOD)
    return comm


def _send_task(comm, X, D, reg, z0, workers_segments, params):
    t_start = time()
    n_jobs = workers_segments.effective_n_seg
    n_atoms, n_channels, *atom_shape = D.shape

    comm.bcast(params, root=MPI.ROOT)
    broadcast_array(comm, D)

    X = np.array(X, dtype='d')
    if params['debug']:
        X_alpha = np.zeros(X.shape, 'd')

    for i_seg in range(n_jobs):
        if params['has_z0']:
            worker_slice = workers_segments.get_seg_slice(i_seg)
            comm.Send([z0[worker_slice].ravel(), MPI.DOUBLE],
                      dest=i_seg, tag=constants.TAG_ROOT + i_seg)
        seg_bounds = workers_segments.get_seg_bounds(i_seg)
        X_worker_slice = (Ellipsis,) + tuple([
            slice(start, end + size_atom_ax - 1)
            for (start, end), size_atom_ax in zip(seg_bounds, atom_shape)
        ])

        comm.Send([X[X_worker_slice].ravel(), MPI.DOUBLE],
                  dest=i_seg, tag=constants.TAG_ROOT + i_seg)
        if params['debug']:
            X_worker = np.empty(X_alpha[X_worker_slice].shape, 'd')
            comm.Recv([X_worker.ravel(), MPI.DOUBLE],
                      source=i_seg, tag=constants.TAG_ROOT + i_seg)
            X_alpha[X_worker_slice] += X_worker

    if params['debug']:
        import matplotlib.pyplot as plt
        plt.imshow(np.clip(X_alpha.swapaxes(0, 2), 0, 1))
        plt.show()
        assert (np.sum(X_alpha[0, 0] == 0.5) ==
                3 * (atom_shape[-1] - 1) *
                (workers_segments.n_seg_per_axis[0] - 1)
                )

    if flags.CHECK_WARM_BETA:
        pt_global = workers_segments.get_seg_shape(0, inner=True)
        msg = np.empty(1, 'd')
        value = []
        for i_worker in range(n_jobs):

            pt = workers_segments.get_local_coordinate(i_worker, pt_global)
            if workers_segments.is_contained_coordinate(i_worker, pt):
                comm.Recv([msg, MPI.DOUBLE], source=i_worker)
                value.append(msg[0])
        if len(value) > 1:
            assert np.allclose(value[1:], value[0]), value

    comm.Barrier()

    t_init = time() - t_start
    if params['verbose'] > 0:
        print('\r[DICOD-{}:INFO] End initialization - {:.4}s'.ljust(80)
              .format(workers_segments.effective_n_seg, t_init))
    return


def _recv_result(comm, D_shape, valid_shape, workers_segments,
                 return_ztz=False, timing=False, verbose=0):
    n_atoms, n_channels, *atom_shape = D_shape

    t_start = time()

    inner = not flags.GET_OVERLAP_Z_HAT

    z_hat = np.empty((n_atoms,) + valid_shape, dtype='d')
    for i_seg in range(workers_segments.effective_n_seg):
        worker_shape = workers_segments.get_seg_shape(
            i_seg, inner=inner)
        worker_slice = workers_segments.get_seg_slice(
            i_seg, inner=inner)
        z_worker = np.zeros((n_atoms,) + worker_shape, 'd')
        comm.Recv([z_worker.ravel(), MPI.DOUBLE], source=i_seg,
                  tag=constants.TAG_ROOT + i_seg)
        z_hat[worker_slice] = z_worker

    if return_ztz:
        ztz_shape = tuple([2 * size_atom_ax - 1
                          for size_atom_ax in atom_shape])
        ztz = np.zeros((n_atoms, n_atoms, *ztz_shape), dtype='d')
        comm.Reduce(None, [ztz, MPI.DOUBLE], MPI.SUM, root=MPI.ROOT)

        ztX = np.zeros((n_atoms, n_channels) + tuple(atom_shape), dtype='d')
        comm.Reduce(None, [ztX, MPI.DOUBLE], MPI.SUM, root=MPI.ROOT)
    else:
        ztz, ztX = None, None

    _log = []
    if timing:
        for i_seg in range(workers_segments.effective_n_seg):
            _log.extend(comm.recv(source=i_seg))

    stats = comm.gather(None, root=MPI.ROOT)
    n_coordinate_updates = np.sum(stats, axis=0)[0]
    runtime = np.max(stats, axis=0)[1]
    if verbose > 0:
        print("\r[DICOD-{}:INFO] converged in {:.3f}s with {:.0f} coordinate "
              "updates.".format(workers_segments.effective_n_seg, runtime,
                                n_coordinate_updates))

    t_reduce = time() - t_start
    if verbose >= 5:
        print('\r[DICOD-{}:DEBUG] End finalization - {:.4}s'
              .format(workers_segments.effective_n_seg, t_reduce))

    return z_hat, ztz, ztX, _log
