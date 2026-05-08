"""End-to-end test example for the Amarth causal pipeline."""

import numpy as np
import pandas as pd
import structlog

from amarth.discovery.ensemble import EnsembleDiscoverer
from amarth.estimation.dowhy import DowhyEstimator
from amarth.estimation.heterogeneous import EmbeddingConfounderEstimator

logger = structlog.get_logger(__name__)


def generate_synthetic_data(n_samples: int = 1000) -> pd.DataFrame:
    """
    Latent structure:
        C1 -> X
        C1 -> Y
        C2 -> Y
        X  -> Y

    True causal effect:
        dY/dX = 1.2
    """

    np.random.seed(42)

    C1 = np.random.normal(0, 1, n_samples)
    C2 = np.random.normal(0, 1, n_samples)

    X = 0.8 * np.sin(C1) + 0.4 * C2 + np.random.normal(0, 0.8, n_samples)

    tau = 1.2 + 0.3 * np.tanh(C1)

    Y = (
        tau * X
        + 0.8 * (C1**2)
        + 0.5 * np.cos(C2)
        + np.random.normal(0, 1.0, n_samples)
    )

    embedding_dim = 32
    embeddings = []

    A = np.random.normal(0, 1, (embedding_dim, 2))

    for i in range(n_samples):
        latent_vec = np.array([C1[i], C2[i]])
        signal = A @ latent_vec
        signal = np.tanh(signal)
        noise = np.random.normal(0, 1.0, embedding_dim)
        emb = signal + noise
        embeddings.append(emb.astype(np.float32))

    df = pd.DataFrame(
        {
            "feature_X": X,
            "outcome_Y": Y,
            "embedding": embeddings,
        }
    )

    return df


def test_full_pipeline():
    df = generate_synthetic_data(n_samples=800)
    df_scalars = df[["feature_X", "outcome_Y"]]

    discoverer = EnsembleDiscoverer(
        notears_threshold=0.3, lingam_threshold=0.05
    )
    dag = discoverer.fit(df_scalars)

    logger.info("discovered_edges", edges=list(dag.edges(data=True)))
    # Should be: [('feature_X', 'outcome_Y', {'weight': ..., 'status': 'confirmed'})]

    estimator = DowhyEstimator(strict_dag=True)
    result_classic = estimator.estimate_effect(
        df=df_scalars, dag=dag, treatment="feature_X", outcome="outcome_Y"
    )

    if result_classic:
        logger.info(
            "classic_estimation_result",
            ate=round(result_classic.ate, 3),
            status=result_classic.edge_status,
        )

    ht_estimator = EmbeddingConfounderEstimator(discrete_treatment=False)
    result_ht = ht_estimator.estimate_effect(
        df=df,
        treatment="feature_X",
        outcome="outcome_Y",
        embedding_col="embedding",
        dag=None,
    )

    if result_ht:
        logger.info(
            "heterogeneous_estimation_result",
            ate=round(result_ht.ate, 3),
            cate_std=round(result_ht.cate_std, 3),
            refutation_passed=result_ht.refutation_passed,
        )


if __name__ == "__main__":
    test_full_pipeline()
