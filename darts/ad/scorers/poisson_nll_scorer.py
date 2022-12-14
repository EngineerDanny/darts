"""
PoissonNLLScorer
-----

Poisson negative log-likelihood Scorer
Source of PDF function and parameters estimation (MLE):  `Poisson distribution
<https://www.statlect.com/fundamentals-of-statistics/Poisson-distribution-maximum-likelihood>`_.
"""

import math

import numpy as np

from darts.ad.scorers.scorers import NLLScorer


class PoissonNLLScorer(NLLScorer):
    def __init__(self, window: int = 1) -> None:
        super().__init__(window=window)

    def __str__(self):
        return "PoissonNLLScorer"

    def _score_core_nllikelihood(
        self,
        deterministic_values: np.ndarray,
        probabilistic_estimations: np.ndarray,
    ) -> np.ndarray:

        # TODO: vectorize

        return [
            -np.log(
                np.exp(-x1.mean()) * (x1.mean() ** x2) / math.factorial(x2.astype(int))
            )
            for (x1, x2) in zip(probabilistic_estimations, deterministic_values)
        ]
