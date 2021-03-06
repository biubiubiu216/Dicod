import numpy as np


from dicod.multivariate_convolutional_coding_problem import\
    MultivariateConvolutionalCodingProblem
from dicod.fista import FISTA


def test_fista_simple():
    K = 3
    rng = np.random.RandomState(42)

    D = rng.randn(K, 2, 5)
    D /= np.sqrt((D*D).sum(axis=-1))[:, :, None]
    z = np.zeros((K, 100))
    z[0, [0, 12, 23, 30, 42, 50, 65, 85, 95]] = 1
    z[1, 67] = 2
    x = np.array([[np.convolve(zk, dk, 'full') for dk in Dk]
                  for Dk, zk in zip(D, z)]).sum(axis=0)
    pb = MultivariateConvolutionalCodingProblem(
            D, x, lmbd=0.002)

    fista = FISTA(fixe=True, max_iter=1e5, debug=5, logging=True)
    fista.fit(pb)

    pt = pb.pt*(abs(pb.pt) > pb.lmbd)

    # Assert we recover the right support
    print(pt.reshape(1, -1).nonzero()[1], '\n',
          z.reshape(1, -1).nonzero()[1])
    assert (np.all(pt.reshape(1, -1).nonzero()[1] ==
                   z.reshape(1, -1).nonzero()[1])), (
        "Cost pt: ", fista.cost, "Cost z: ", pb.cost(z))
