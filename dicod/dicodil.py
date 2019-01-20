
import sys
import time
import numpy as np

from alphacsc.update_d_multi import update_d
from alphacsc.init_dict import get_max_error_dict
from alphacsc.utils.dictionary import get_lambda_max

from .utils import constants
from .utils.segmentation import Segmentation
from .workers.reusable_workers import get_reusable_workers
from .utils.mpi import broadcast_array, recv_reduce_sum_array
from .workers.reusable_workers import send_command_to_reusable_workers

from ._dicod import _send_task, _find_grid_size, _collect_end_stat
from ._dicod import recv_cost, recv_z_hat, recv_sufficient_statistics


DEFAULT_DICOD_KWARGS = dict(max_iter=int(1e8), timeout=None)


def dicodil(X, D_hat, reg=.1, z_positive=True, n_iter=100, strategy='greedy',
            n_seg='auto', tol=1e-1, dicod_kwargs=DEFAULT_DICOD_KWARGS,
            w_world='auto', n_jobs=4, hostfile=None, stopping_pobj=None,
            eps=1e-5, raise_on_increase=True, random_state=None,
            name="DICODIL", verbose=0):

    lmbd_max = get_lambda_max(X[None], D_hat).max()
    if verbose > 5:
        print("[DICODIL:DEBUG] Lambda_max = {}".format(lmbd_max))
    reg_ = reg * lmbd_max

    params = dicod_kwargs.copy()
    params.update(dict(
        strategy=strategy, n_seg=n_seg, z_positive=z_positive, tol=tol,
        random_state=random_state, reg=reg_, verbose=verbose, timing=False,
        use_soft_lock=True, has_z0=True, return_ztz=False,
        freeze_support=False, debug=False,

    ))

    n_channels, *sig_shape = X.shape
    n_atoms, n_channels, *atom_shape = D_hat.shape
    assert D_hat.ndim - 1 == X.ndim

    params['valid_shape'] = valid_shape = tuple([
        size_ax - size_atom_ax + 1
        for size_ax, size_atom_ax in zip(sig_shape, atom_shape)
    ])
    overlap = tuple([size_atom_ax - 1 for size_atom_ax in atom_shape])
    z_size = n_atoms * np.prod(valid_shape)

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

    # Initialize constants dictionary
    constants = {}
    constants['n_channels'] = X.shape[1]
    constants['XtX'] = np.dot(X.ravel(), X.ravel())

    z0 = np.zeros((n_atoms, *valid_shape))

    comm = _request_workers(n_jobs, hostfile)
    t_init = _send_task(comm, X, D_hat, reg_, z0, workers_segments, params)

    # monitor cost function
    t_start = time.time()
    times = [0]
    pobj = [compute_cost(comm)]

    for ii in range(n_iter):  # outer loop of coordinate descent
        if verbose == 1:
            msg = '.' if ((ii + 1) % 50 != 0) else '+\n'
            print(msg, end='')
            sys.stdout.flush()
        if verbose > 1:
            print('[{}:INFO] {:.0f}s - CD iterations {} / {}'
                  .format(name, time.time() - t_start, ii, n_iter))

        if verbose > 5:
            print('[{}:DEBUG] lambda = {:.3e}'.format(name, np.mean(reg_)))

        # Compute z update
        t_start_update_z = time.time()
        update_z(comm, n_jobs, verbose=verbose)
        constants['ztz'], constants['ztX'] = get_sufficient_statistics(
            comm, D_hat.shape)

        # monitor cost function
        times.append(time.time() - t_start_update_z)
        pobj.append(compute_cost(comm))

        z_nnz = get_z_nnz(comm, n_atoms)
        if verbose > 5:
            print("[{}:DEBUG] sparsity: {:.3e}".format(
                name, z_nnz.sum() / z_size))
            print('[{}:DEBUG] Objective (z) : {:.3e}'.format(name, pobj[-1]))

        if np.all(z_nnz == 0):
            import warnings
            warnings.warn("Regularization parameter `reg` is too large "
                          "and all the activations are zero. No atoms has"
                          " been learned.", UserWarning)
            break

        # Compute D update
        t_start_update_d = time.time()
        D_hat = update_d(X, None, D_hat, constants, verbose=verbose)
        update_worker_D(comm, D_hat)
        # monitor cost function
        times.append(time.time() - t_start_update_d)
        pobj.append(compute_cost(comm))

        null_atom_indices = np.where(z_nnz == 0)[0]
        if len(null_atom_indices) > 0:
            k0 = null_atom_indices[0]
            z_hat = get_z_hat(comm, n_atoms, workers_segments)
            D_hat[k0] = get_max_error_dict(X[None], z_hat[None], D_hat,
                                           window=False)[0]
            if verbose > 1:
                print('[{}:INFO] Resampled atom {}'.format(name, k0))

        if verbose > 5:
            print('[{}:DEBUG] Objective (d) : {:.3e}'.format(name, pobj[-1]))

        # Only check that the cost is always going down when the regularization
        # parameter is fixed.
        dz = (pobj[-3] - pobj[-2]) / min(pobj[-3], pobj[-2])
        du = (pobj[-2] - pobj[-1]) / min(pobj[-2], pobj[-1])
        if (dz < eps or du < eps):
            if dz < 0 and raise_on_increase:
                raise RuntimeError(
                    "The z update have increased the objective value by {}."
                    .format(dz))
            if du < -1e-10 and dz > 1e-12 and raise_on_increase:
                raise RuntimeError(
                    "The d update have increased the objective value by {}."
                    "(dz={})".format(du, dz))
            if dz < eps and du < eps:
                if verbose == 1:
                    print("")
                print("[{}:INFO] Converged after {} iteration, (dz, du) "
                      "= {:.3e}, {:.3e}".format(name, ii + 1, dz, du))
                break

        if stopping_pobj is not None and pobj[-1] < stopping_pobj:
            break

    update_z(comm, n_jobs, verbose=verbose)
    z_hat = get_z_hat(comm, n_atoms, workers_segments)
    pobj.append(compute_cost(comm))

    runtime = np.sum(times)
    _release_workers()
    print("[{}:INFO] Finished in {:.0f}s".format(name, runtime))
    return pobj, times, D_hat, z_hat


def _request_workers(n_jobs, hostfile):
    comm = get_reusable_workers(n_jobs, hostfile=hostfile)
    send_command_to_reusable_workers(constants.TAG_WORKER_RUN_DICODIL)
    return comm


def _release_workers():
    send_command_to_reusable_workers(constants.TAG_DICODIL_STOP)


def update_worker_D(comm, D):
    send_command_to_reusable_workers(constants.TAG_DICODIL_UPDATE_D)
    broadcast_array(comm, D)


def update_z(comm, n_jobs, verbose=0):
    send_command_to_reusable_workers(constants.TAG_DICODIL_UPDATE_Z)
    # Wait first for the end of the initialization
    comm.Barrier()
    # Then first for the end of the computation
    comm.Barrier()
    _collect_end_stat(comm, n_jobs, verbose=verbose)


def compute_cost(comm):
    send_command_to_reusable_workers(constants.TAG_DICODIL_GET_COST)
    return recv_cost(comm)


def get_z_hat(comm, n_atoms, workers_segments):
    send_command_to_reusable_workers(constants.TAG_DICODIL_GET_Z_HAT)
    return recv_z_hat(comm, n_atoms, workers_segments)


def get_z_nnz(comm, n_atoms):
    send_command_to_reusable_workers(constants.TAG_DICODIL_GET_Z_NNZ)
    return recv_reduce_sum_array(comm, n_atoms)


def get_sufficient_statistics(comm, D_shape):
    send_command_to_reusable_workers(constants.TAG_DICODIL_GET_SUFFICIENT_STAT)
    return recv_sufficient_statistics(comm, D_shape)
