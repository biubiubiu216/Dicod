#!/usr/bin/env python
import logging
import numpy as np
from time import time
from mpi4py import MPI


from ._lasso_solver import _LassoSolver
from .c_dicod.mpi_pool import get_reusable_pool


log = logging.getLogger('dicod')

ALGO_GS = 0
ALGO_RANDOM = 1


class DICOD(_LassoSolver):
    """MPI implementation of the distributed convolutional pursuit

    Parameters
    ----------
    n_jobs: int, optional (default: 1)
        Maximal number of process to solve this problem
    use_seg: int, optional (default: 1)
        If >1, further segment the updates and update
        the best coordinate over each segment cyclically
    hostfile: str, optional (default: None)
        Specify a hostfile for mpi launch, permit to use
        more than one computer
    logging: bool, optional (default: False)
        Enable the logging of the updates to allow printing a
        cost curve
    debug: int, optional (default: 0)
        verbosity level

    kwargs
    ------
    tol: float, default: 1e-10
    max_iter: int, default: 1000
    timeout: int default: 40

    """

    def __init__(self, n_jobs=1, use_seg=1, hostfile=None,
                 logging=False, debug=0, positive=False,
                 algorithm=ALGO_GS, patience=1000, **kwargs):
        super(DICOD, self).__init__(debug=debug, **kwargs)
        self.debug = debug
        self.n_jobs = n_jobs
        self.hostfile = hostfile
        self.logging = 1 if logging else 0
        self.use_seg = use_seg
        self.positive = 1 if positive else 0
        self.algorithm = algorithm
        self.patience = 1000
        if self.name == '_GD' + str(self.id):
            self.name = 'MPI_DCP' + str(self.n_jobs) + '_' + str(self.id)

    def fit(self, pb):
        self.reset()
        self.pb = pb
        self._init_pool()
        self.end()
        return self.pb.DD

    def _init_pool(self):
        '''Launch n_jobs process to compute the convolutional
        coding solution with MPI process
        '''
        # Rename to call local variables
        self.K, self.d, self.S = self.pb.D.shape

        # Create a pool of worker
        t_start_init_pool = time()
        self._pool = get_reusable_pool(self.n_jobs, self.hostfile)
        self.comm = self._pool.comm
        msg = np.array([3] * 4).astype('i')  # Construct start message
        self._pool.mng_bcast(msg)
        self.t_init_pool = time() - t_start_init_pool
        log.debug('Created pool of worker in {:.4}s'.format(self.t_init_pool))

        # Send the job to process
        self.t_start = time()
        self.send_task()

    def send_task(self):
        self.K, self.d, self.S = self.pb.D.shape
        pb = self.pb
        K, d, S = self.K, self.d, self.S
        T = pb.x.shape[1]
        L = T - S + 1

        # Share constants
        assert self.pb.DD is not None
        alpha_k = np.sum(np.mean(pb.D * pb.D, axis=1), axis=1)
        alpha_k += (alpha_k == 0)

        self._broadcast_array(alpha_k)
        self._broadcast_array(pb.DD)
        self._broadcast_array(pb.D)

        # Send the constants of the algorithm
        max_iter = max(1, self.max_iter // self.n_jobs)
        N = np.array([float(d), float(K), float(S), float(T),
                      self.pb.lmbd, self.tol, float(self.timeout),
                      float(max_iter), float(self.debug), float(self.logging),
                      float(self.use_seg), float(self.positive),
                      float(self.algorithm), float(self.patience)],
                     'd')
        self._broadcast_array(N)

        # Share the work between the processes
        sig = np.array(pb.x, dtype='d')
        L_proc = L // self.n_jobs + 1
        expect = []
        for i in range(self.n_jobs):
            end = min(T, (i + 1) * L_proc + S - 1)
            self.comm.Send([sig[:, i * L_proc:end].flatten(),
                            MPI.DOUBLE], i, tag=100 + i)
            expect += [sig[0, i * L_proc], sig[-1, end - 1]]
        self._confirm_array(expect)
        self.L, self.L_proc = L, L_proc

        # Wait end of initialisation
        self.comm.Barrier()
        self.t_init = time() - self.t_start
        log.debug('End initialisation - {:.4}s'.format(self.t_init))

    def end(self):
        # reduce_pt
        self.comm.Barrier()
        log.debug("End computation, gather result")

        self._gather()

        log.debug("DICOD - Clean end")

    def _gather(self):
        K, L, L_proc = self.K, self.L, self.L_proc
        pt = np.empty((K, L), 'd')

        for i in range(self.n_jobs):
            off = i*self.L_proc
            L_proc_i = min(off+L_proc, L)-off
            gpt = np.empty(K*L_proc_i, 'd')
            self.comm.Recv([gpt, MPI.DOUBLE], i, tag=200+i)
            pt[:, i*L_proc:(i+1)*L_proc] = gpt.reshape((K, -1))

        cost = np.empty(self.n_jobs, 'd')
        iterations = np.empty(self.n_jobs, 'i')
        times = np.empty(self.n_jobs, 'd')
        init_times = np.empty(self.n_jobs, 'd')
        self.comm.Gather(None, [cost, MPI.DOUBLE],
                         root=MPI.ROOT)
        self.comm.Gather(None, [iterations, MPI.INT],
                         root=MPI.ROOT)
        self.comm.Gather(None, [times, MPI.DOUBLE],
                         root=MPI.ROOT)
        self.comm.Gather(None, [init_times, MPI.DOUBLE],
                         root=MPI.ROOT)
        self.cost = np.sum(cost)
        self.iteration = np.sum(iterations)
        self.time = times.max()
        log.debug("Iterations {}".format(iterations))
        log.debug("Times {}".format(times))
        log.debug("Cost {}".format(cost))
        self.pb.pt = pt
        self.pt_dbg = np.copy(pt)
        log.info('End for {} : iteration {}, time {:.4}s'
                 .format(self, self.iteration, self.time))

        if self.logging:
            self._log(iterations)

        self.comm.Barrier()
        self.runtime = time()-self.t_start
        log.debug('Total time: {:.4}s'.format(self.runtime))

    def _log(self, iterations):
        self.comm.Barrier()
        pb, L = self.pb, self.L
        updates, updates_t, updates_skip = [], [], []
        for id_worker, n_iter in enumerate(iterations):
            _log = np.empty(4 * n_iter)
            self.comm.Recv([_log, MPI.DOUBLE], id_worker, tag=300 + id_worker)
            updates += [(int(_log[4 * i]), _log[4 * i + 2])
                        for i in range(n_iter)]
            updates_t += [_log[4 * i + 1] for i in range(n_iter)]
            updates_skip += [_log[4 * i + 3] for i in range(n_iter)]

        i0 = np.argsort(updates_t)
        self.next_log = 1
        pb.reset()
        log.debug('Start logging cost')
        t = self.t_init
        it = 0
        for i in i0:
            if it + 1 >= self.next_log:
                self.record(it, t, pb.cost(pb.pt))
            j, du = updates[i]
            t = updates_t[i] + self.t_init
            pb.pt[j // L, j % L] += du
            it += 1 + updates_skip[i]
        self.log_update = (updates_t, updates)
        log.debug('End logging cost')

    def gather_AB(self):
        K, S, d = self.K, self.S, self.d
        A = np.empty(K*K*S, 'd')
        B = np.empty(d*K*S, 'd')
        self.comm.Barrier()
        log.debug("End computation, gather result")

        self.comm.Reduce(None, [A, MPI.DOUBLE], op=MPI.SUM,
                         root=MPI.ROOT)
        self.comm.Reduce(None, [B, MPI.DOUBLE], op=MPI.SUM,
                         root=MPI.ROOT)

        iterations = np.empty(self.n_jobs, 'i')
        self.comm.Gather(None, [iterations, MPI.INT],
                         root=MPI.ROOT)
        self.iteration = np.sum(iterations)
        log.debug("Iterations {}".format(iterations))

        self.comm.Barrier()
        self.gather()
        return A, B

    def _broadcast_array(self, arr):
        arr = np.array(arr).flatten().astype('d')
        T = arr.shape[0]
        N = np.array(T, 'i')
        self.comm.Bcast([N, MPI.INT], root=MPI.ROOT)
        # self.comm.Bcast(N, root=MPI.ROOT)
        self.comm.Bcast([arr, MPI.DOUBLE], root=MPI.ROOT)

    def _confirm_array(self, expect):
        '''Aux function to confirm that we passed the correct array
        '''
        expect = np.array(expect)
        gathering = np.empty(expect.shape, 'd')
        self.comm.Gather(None, [gathering, MPI.DOUBLE],
                         root=MPI.ROOT)
        assert (np.allclose(expect, gathering)), (
            expect, gathering, 'Fail to transmit array')

    def p_update(self):
        return 0

    def _stop(self, dz):
        return True
