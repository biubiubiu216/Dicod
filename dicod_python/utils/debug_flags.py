
# Set the debug flags to True when testing dicod.
import os
TESTING_DICOD = os.environ.get("TESTING_DICOD", False)


# Start interactive child processes when set to True
INTERACTIVE_PROCESSES = False

# If set to True, check that inactive segments do not have any coefficient
# with update over tol.
CHECK_ACTIVE_SEGMENTS = TESTING_DICOD


# If set to True, check that the updates selected have indeed an impact only
# on the coefficients that are contained in the worker.
CHECK_UPDATE_CONTAINED = TESTING_DICOD


# If set to True, check that beta is consistent with z_hat after each update
# from a neighbor.
CHECK_BETA = TESTING_DICOD


# If set to True, request the full z_hat from each worker. It should not change
# the resulting solution.
GET_OVERLAP_Z_HAT = TESTING_DICOD
