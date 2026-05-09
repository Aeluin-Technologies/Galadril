import numpy as np
import pandas as pd
import networkx as nx
import structlog
from amarth.router import AmarthRouter

logger = structlog.get_logger(__name__)


def generate_detectable_adversarial_scenario(days: int = 400) -> pd.DataFrame:
    np.random.seed(42)
    t = pd.date_range(start="2026-01-01", periods=days, freq="D")
    tension = np.sin(np.linspace(0, 6 * np.pi, days))
    patrol = 15.0 + (1.0 * tension) + np.random.normal(0, 1.0, days)

    incident_rate = np.zeros(days)
    ate_truth = []

    for i in range(days):
        current_ate = -5.0 if i < 200 else -2.0
        ate_truth.append(current_ate)
        p_eff = patrol[i - 1] if i > 0 else 15.0

        incident_rate[i] = max(
            0,
            100.0
            + (current_ate * p_eff)
            + (2.0 * tension[i])
            + np.random.normal(0, 0.5),
        )

    embeddings = [
        np.array(
            [
                tension[i] + np.random.normal(0, 0.1),
                np.random.random(),
                tension[i] ** 2,
            ]
        )
        for i in range(days)
    ]

    return pd.DataFrame(
        {
            "timestamp": t,
            "patrol_intensity": patrol,
            "incident_rate": incident_rate,
            "intel_embedding": embeddings,
            "true_ate_reference": ate_truth,
        }
    )


def test_robustness_pipeline():
    df = generate_detectable_adversarial_scenario()

    prior = nx.DiGraph()
    prior.add_edge("patrol_intensity", "incident_rate")
    router = AmarthRouter(strict_dag=True)

    try:
        results = router.analyze(
            df=df,
            target_outcome="incident_rate",
            time_col="timestamp",
            embedding_col="intel_embedding",
            prior_graph=prior,
            window_size="200D",
        )

        effects = results.get("causal_effects", [])
        patrol_effect = next(
            (e for e in effects if e.treatment == "patrol_intensity"), None
        )

        inferred_params = results.get("metadata", {}).get("inferred_params", {})
        window_used = inferred_params.get("window_size")
        print(f"Window Size Used : {window_used}")
        print(f"Stability Threshold: {inferred_params.get('stability')}")

        if patrol_effect:
            expected_mean = -2
            accuracy = 1 - (
                abs(patrol_effect.ate - expected_mean) / abs(expected_mean)
            )

            print(f"Estimated ATE : {patrol_effect.ate:.3f}")
            print(f"Target Mean   : {expected_mean:.3f}")
            print(f"Model Accuracy: {accuracy:.1%}")
        else:
            print(" STATUS: FAILURE. Still no causal effect detected.")

    except Exception as e:
        print(f"Pipeline Error: {e}")


if __name__ == "__main__":
    test_robustness_pipeline()
