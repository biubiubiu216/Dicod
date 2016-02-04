#!/usr/bin/env python
from mpi4py import MPI
import numpy as np
from time import time
from toolbox.optim import _GradientDescent
from os import path
from .c_dicod.mpi_pool import get_reusable_pool

from toolbox.logger import Logger
log = Logger('MPI_DCP')

ALGO_GS = 0
ALGO_RANDOM = 1

TAG_ROOT = 4242


class DICOD2D(_GradientDescent):
    """MPI implementation of the distributed convolutional pursuit

    Parameters
    ----------
    pb: toolbox.optim._Problem
        convolutional coding problem
    n_jobs: int, optional (default: 1)
        Maximal number of process to solve this problem
    use_seg: int, optional (default: 1)
        If >1, further segment the updates and update
        the best coordinate over each semgnet cyclically
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
    i_max: int, default: 1000
    t_max: int default: 40

    """

    def __init__(self, pb, n_jobs=1, w_world=1, use_seg=1, hostfile=None,
                 logging=False, debug=0, positive=False,
                 algorithm=ALGO_GS, patience=1000, **kwargs):
        log.set_level(max(3-debug, 1)*10)
        debug = max(debug-1, 0)
        super(DICOD2D, self).__init__(pb, debug=debug, **kwargs)
        self.debug = debug
        self.n_jobs = n_jobs
        self.hostfile = hostfile
        self.logging = 1 if logging else 0
        self.use_seg = use_seg
        self.positive = 1 if positive else 0
        self.algorithm = algorithm
        self.patience = 1000
        if self.name == '_GD'+str(self.id):
            self.name = 'MPI_DCP' + str(self.n_jobs) + '_' + str(self.id)
        print('Logger', log.level)

        self.w_world = w_world
        self.h_world = self.n_jobs // self.w_world

    def fit(self, pb, DD=None):
        self.pb = pb
        DD = self._init_pool(DD=DD)
        self.end()
        return self.pb.DD

    def _init_pool(self, DD=None):
        '''Launch n_jobs process to compute the convolutional
        coding solution with MPI process
        '''
        # Rename to call local variables
        self.K, self.d, self.h_dic, self.w_dic = self.pb.D.shape

        # Create a pool of worker
        t = time()
        '''mpi_info = MPI.Info.Create()
        if self.hostfile is not None:
            mpi_info.Set("add-hostfile", self.hostfile)
            mpi_info.Set("map_bynode", '1')
        c_prog = path.dirname(path.abspath(__file__))
        c_prog = path.join(c_prog, 'c_dicod', 'c_dicod')
        self.comm = MPI.COMM_SELF.Spawn(c_prog, maxprocs=self.n_jobs,
                                        info=mpi_info)'''
        self._pool = get_reusable_pool(self.n_jobs, self.hostfile)
        self.comm = self._pool.comm
        self._pool.mng_bcast(np.array([4]*4).astype('i'))
        log.debug('Created pool of worker in {:.4}s'.format(time()-t))

        # Send the job to process
        self.send_task(DD)

    def send_task(self, DD=None):
        self.K, self.d, self.h_dic, self.w_dic = self.pb.D.shape
        self.t_start = time()
        pb = self.pb
        K, d, h_dic, w_dic = self.K, self.d, self.h_dic, self.w_dic
        h_sig, w_sig = pb.x.shape[1:]
        h_cod = h_sig-h_dic+1
        w_cod = w_sig-w_dic+1

        # Share constants
        pb.compute_DD(DD=DD)
        alpha_k = np.sum(np.mean(pb.D*pb.D, axis=1), axis=-1).sum(axis=-1)
        alpha_k += (alpha_k == 0)
        self.t_init = time() - self.t_start

        self._broadcast_array(alpha_k)
        self._broadcast_array(pb.DD)
        self._broadcast_array(pb.D)

        w_world = self.w_world
        h_world = self.n_jobs // w_world
        assert self.n_jobs % self.w_world == 0

        # Send the constants of the algorithm
        N = np.array([float(d), float(K), float(h_dic), float(w_dic),
                      float(h_sig), float(w_sig), float(w_world),
                      self.pb.lmbd, self.tol, float(self.t_max),
                      self.i_max/self.n_jobs, float(self.debug),
                      float(self.logging), float(self.use_seg),
                      float(self.positive), float(self.algorithm),
                      float(self.patience)],
                     'd')
        self._broadcast_array(N)

        # Share the work between the processes
        sig = np.array(pb.x, dtype='d')
        h_proc = h_cod // h_world
        w_proc = w_cod // w_world + 1
        expect = []
        for i in range(h_world):
            for j in range(w_world):
                dest = i*w_world+j
                h_end = min(h_sig, (i+1)*h_proc+h_dic-1)
                w_end = min(w_sig, (j+1)*w_proc+w_dic-1)
                self.comm.Send([sig[:, i*h_proc:h_end,
                                    j*w_proc:w_end].flatten(),
                                MPI.DOUBLE], dest, tag=TAG_ROOT+dest)
                expect += [sig[0, i*h_proc, j*w_proc],
                           sig[-1, h_end-1, w_end-1]]
        self.t_start = time()
        self._confirm_array(expect)
        self.h_cod, self.h_proc = h_cod, h_proc
        self.w_cod, self.w_proc = w_cod, w_proc
        self.L = h_cod*w_cod

        # Wait end of initialisation
        self.comm.Barrier()
        self.t_init = time() - self.t_start
        log.debug('End initialisation - {:.4}s'.format(self.t_init))

    def end(self):
        # reduce_pt
        self._gather()
        if type(self.t) == int:
            self.t = time()-self.t_start
        return

        log.debug("DICOD - Clean end")
        self.comm.Disconnect()

    def _gather(self):
        K = self.K
        h_cod, h_proc = self.h_cod, self.h_proc
        w_cod, w_proc = self.w_cod, self.w_proc
        pt = np.empty((K, h_cod, w_cod), 'd')
        self.comm.Barrier()
        log.debug("End computation, gather result")
        self.t = time()-self.t_start

        for i in range(self.h_world):
            for j in range(self.w_world):
                src = i*self.w_world+j
                h_off = i*h_proc
                w_off = j*w_proc
                h_proc_i = min(h_off+h_proc, h_cod)-h_off
                w_proc_i = min(w_off+w_proc, w_cod)-w_off
                gpt = np.empty(K*h_proc_i*w_proc_i, 'd')
                self.comm.Recv([gpt, MPI.DOUBLE], src, tag=TAG_ROOT+src)
                pt[:, i*h_proc:(i+1)*h_proc, j*w_proc:(j+1)*w_proc] = \
                    gpt.reshape((K, h_proc_i, w_proc_i))

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
        self.t_init += max(init_times)
        self.cost = np.sum(cost)
        self.iteration = np.sum(iterations)
        self.runtime = times.max()
        log.debug("Iterations", iterations)
        log.debug("Times", times)
        t_end = time()
        self.pb.pt = pt
        self.pt_dbg = np.copy(pt)
        log.info('End for {}'.format(self),
                 'iteration {}, time {:.4}s'.format(self.iteration, self.t))
        log.debug('Total time: {:.4}s'.format(time()-self.t_start))
        log.debug('Total time: {:.4}s'.format(self.runtime))
        self.runtime += self.t_init

        if self.logging:
            self._log(iterations, t_end)

        self.comm.Barrier()
        log.info("Conv sparse coding end in {:.4}s for {} iterations"
                 "".format(self.runtime, self.iteration))

    def _log(self, iterations, t_end):
        pb, L = self.pb, self.L
        updates = []
        updates_t = []
        for i, it in enumerate(iterations):
            _log = np.empty(3*it)
            self.comm.Recv([_log, MPI.DOUBLE], i, tag=TAG_ROOT+i)
            updates += [(round(_log[3*i]), _log[3*i+2]) for i in range(it)]
            updates_t += [_log[3*i+1] for i in range(it)]

        i0 = np.argsort(updates_t)
        next_log = 1
        pb.reset()
        log.debug('Start logging cost')
        t = self.t_init
        for it, i in enumerate(i0):
            if it+1 >= next_log:
                log.log_obj(name='cost'+str(self.id), obj=np.copy(pb.pt),
                            iteration=it+1, fun=pb.cost,
                            graph_cost=self.graph_cost, time=t)
                next_log = self.log_rate(it+1)
            j, du = updates[i]
            t = updates_t[i]+self.t_init
            t0 = j % L
            pb.pt[j // L, t0 // self.w_cod, t0 % self.w_cod] += du
        log.log_obj(name='cost'+str(self.id), obj=np.copy(pb.pt),
                    iteration=it, fun=pb.cost,
                    graph_cost=self.graph_cost, time=self.runtime+self.t_init)
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
        log.debug("Iterations", iterations)

        self.comm.Barrier()
        self.gather()
        return A, B

    def _broadcast_array(self, arr):
        arr = np.array(arr).astype('d')
        T = np.prod(arr.shape)
        N = np.array(T, 'i')
        self.comm.Bcast([N, MPI.INT], root=MPI.ROOT)
        self.comm.Bcast([arr.flatten(), MPI.DOUBLE], root=MPI.ROOT)

    def _confirm_array(self, expect):
        '''Aux function to confirm that we passed the correct array
        '''
        expect = np.array(expect)
        gathering = np.empty(expect.shape, 'd')
        self.comm.Gather(None, [gathering, MPI.DOUBLE],
                         root=MPI.ROOT)
        assert (np.allclose(expect, gathering)), (expect, gathering,
                                                  'Fail to transmit array')

    def p_update(self):
        return 0

    def _stop(self, dz):
        return True