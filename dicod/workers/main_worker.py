"""Main script for MPI workers

Author : tommoral <thomas.moreau@inria.fr>
"""
from dicod.utils import constants
from dicod.workers.dicod_worker import DICODWorker
from dicod.workers.dicodil_worker import dicodil_worker
from dicod.utils.mpi import wait_message, sync_workers


def main():
    sync_workers()
    tag = wait_message()
    while tag != constants.TAG_WORKER_STOP:
        if tag == constants.TAG_WORKER_RUN_DICOD:
            dicod = DICODWorker(backend='mpi')
            dicod.run()
        if tag == constants.TAG_WORKER_RUN_DICODIL:
            dicodil_worker()
        tag = wait_message()


if __name__ == "__main__":
    main()
