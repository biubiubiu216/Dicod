import datetime
import numpy as np
import os.path as osp
from time import sleep

from cdl.dicod import DICOD
from utils.rand_problem import fun_rand_problem


def iter_njobs(T=300, max_jobs=75, n_rep=10, save_dir=None,
               i_max=5e6, t_max=7200,  hostfile=None, run='all',
               lgg=False, graphical_cost=None, debug=0, seed=None):
    '''Run DICOD algorithm for a certain problem with different value
    for n_jobs and store the runtime in csv files if given a save_dir.

    Parameters
    ----------
    T: int, optional (default: 300)
        Size of the generated problems
    max_jobs: int, optional (default: 75)
        The algorithm will be run on problems with a number
        of cores varying from 5 to max_jobs in a log2 fashion
    n_rep: int, optional (default: 10)
        Number of different problem solved for all the different
        number of cores.
    save_dir: str, optional (default: None)
        If not None, all the runtimes will be saved in csv files
        contained in the given directory. The directory must exist.
        This will create a file for each problem size T and save
        the Pb number, the number of core and the runtime computed
        in two different ways.
    i_max: int, optional (default: 5e6)
        maximal number of iteration run by DICOD
    t_max: int, optional (default: 7200)
        maximal running time for DICOD. The default timeout
        is 2 hours
    hostfile: str, optional (default: None)
        hostfile for the openMPI API to connect to the other
        running server to spawn the processes over different
        nodes
    run: list or str, optional (default: 'all')
        if all, run all the possible runs. Else, it should be a list composed
        of int for njobs to run or of str {n_jobs:nrep} for specific cases
    lgg: bool, optional (default: False)
        If set to true, enable the logging of the iteration cost
        during the run. It might slow down a bit the execution
        time and the collection of the results
    graphical_cost: dict, optional (default: None)
        Setup option to enable a grpahical logging of the cost
        function.
    debug: int, optional (default:0)
        The greater it is, the more verbose the algorithm
    seed: int, optional (default:None)
        seed the rng of numpy to obtain fixed set of problems

    '''
    common_args = dict(logging=lgg, log_rate='log1.6', i_max=i_max,
                       t_max=t_max, graphical_cost=graphical_cost,
                       debug=debug, tol=5e-2, hostfile=hostfile)
    S = 150
    K = 10
    d = 7
    lmbd = 0.1
    noise_level = 1

    if save_dir is not None and not osp.exists(save_dir):
        import os
        os.mkdir(save_dir)

    rng = np.random.RandomState(seed)

    for j in range(n_rep):
        seed_pb = rng.randint(4294967295)
        pb = fun_rand_problem(T, S, K, d, lmbd, noise_level, seed=seed_pb)

        dcp = DICOD(pb, n_jobs=2,
                    **common_args)

        runtimes = []
        n_jobs = np.logspace(0, np.log2(75), 10, base=2)
        n_jobs = [int(round(nj)) for nj in n_jobs if nj <= max_jobs]
        n_jobs = np.unique(n_jobs)
        n_jobs = n_jobs[::-1]
        for nj in n_jobs:
            code_run = "{}:{}".format(nj, j)
            if (run != 'all' and nj not in run and code_run not in run):
                continue
            if j < 5:
                continue
            dcp.reset()
            pb.reset()
            dcp.n_jobs = nj

            dcp.fit(pb)
            runtimes += [[dcp.runtime, dcp.t]]
            import time
            time.sleep(1)
            rt = runtimes[-1]
            if save_dir is not None:
                with open(osp.join(save_dir, 'runtimes_{}.csv'.format(T)),
                          'a') as f:
                    f.write('Pb{},{},{},{}\n'.format(
                        j, nj, rt[0], rt[1]))
            print('='*79)
            print('[{}] PB{}: End process with {} jobs  in {:.2f}s'
                  ''.format(datetime.datetime.now().strftime("%I:%M"),
                            j, nj, rt[0]))
            print('\n'+'='*79)
            sleep(.5)
