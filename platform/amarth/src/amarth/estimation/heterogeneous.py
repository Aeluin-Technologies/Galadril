"""Heterogeneous treatment effect estimation using DoWhy and EconML.

Uses Double Machine Learning (DML) with LightGBM to handle high-dimensional
embeddings (e.g., pgvector) as confounders while avoiding statistical bias.
"""

import warnings
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd
import networkx as nx
import lightgbm as lgb
from dowhy import CausalModel
import structlog

from sklearn.exceptions import DataConversionWarning

warnings.filterwarnings(action="ignore", category=DataConversionWarning)
warnings.filterwarnings(
    action="ignore",
    category=UserWarning,
    message="X does not have valid feature names",
)
warnings.filterwarnings(
    "ignore", message="Co-variance matrix is underdetermined.*"
)

logger = structlog.get_logger(__name__)


@dataclass
class HeterogeneousEstimateResult:
    """Container for Heterogeneous Causal Estimation results."""

    treatment: str
    outcome: str
    ate: float
    cate_std: float
    refutation_passed: bool
    summary: str
    p_value: float | None


class EmbeddingConfounderEstimator:
    """Estimates causal effects controlling for high-dimensional embeddings."""

    def __init__(self, discrete_treatment: bool = False):
        """Initializes the HTE estimator.

        Args:
            discrete_treatment: True if the treatment is categorical/boolean.
        """
        self.discrete_treatment = discrete_treatment

    def estimate_effect(
        self,
        df: pd.DataFrame,
        treatment: str,
        outcome: str,
        embedding_col: str,
        dag: Optional[nx.DiGraph] = None,
    ) -> Optional[HeterogeneousEstimateResult]:
        """Estimates the causal effect controlling for the embedding vector."""
        n_samples = len(df)
        df_processed, emb_feature_names = self._unpack_embeddings(
            df, embedding_col
        )

        logger.info(
            "embeddings_unpacked",
            dimensions=len(emb_feature_names),
            samples=n_samples,
        )

        confounders = set(emb_feature_names)
        if dag is not None:
            if treatment in dag:
                confounders.update(dag.predecessors(treatment))
            if outcome in dag:
                confounders.update(dag.predecessors(outcome))

            confounders.discard(treatment)
            confounders.discard(outcome)

            if treatment in dag:
                mediators = nx.descendants(dag, treatment)
                confounders -= mediators

        model = CausalModel(
            data=df_processed,
            treatment=treatment,
            outcome=outcome,
            common_causes=list(confounders),
            effect_modifiers=emb_feature_names,
            graph=None,
        )

        try:
            identified_estimand = model.identify_effect(
                proceed_when_unidentifiable=False
            )
        except ValueError as e:
            logger.error("unidentifiable_effect", error=str(e))
            return None

        model_y, model_t, cv_folds = self._build_nuisance_models(n_samples)

        logger.info(
            "fitting_dml_model_lgbm",
            treatment=treatment,
            outcome=outcome,
            cv_folds=cv_folds,
        )

        estimate = model.estimate_effect(
            identified_estimand,
            method_name="backdoor.econml.dml.LinearDML",
            method_params={
                "init_params": {
                    "model_y": model_y,
                    "model_t": model_t,
                    "discrete_treatment": self.discrete_treatment,
                    "linear_first_stages": False,
                    "cv": cv_folds,
                },
                "fit_params": {
                    "inference": "bootstrap",
                },
            },
        )

        cates = estimate.estimator.effect(df_processed[emb_feature_names])
        ate = np.mean(cates)
        cate_std = np.std(cates)

        p_value = None
        try:
            raw_p = estimate.estimator.ate_pvalue()
            p_value = (
                float(raw_p[0])
                if isinstance(raw_p, (np.ndarray, list))
                else float(raw_p)
            )
        except Exception as e:
            logger.warning("failed_to_extract_p_value", error=str(e))

        refute = model.refute_estimate(
            identified_estimand,
            estimate,
            method_name="data_subset_refuter",
            subset_fraction=0.8,
        )

        return HeterogeneousEstimateResult(
            treatment=treatment,
            outcome=outcome,
            ate=float(ate),
            cate_std=float(cate_std),
            refutation_passed=(refute.new_effect is not None),
            summary=str(estimate),
            p_value=p_value,
        )

    def _build_nuisance_models(self, n_samples: int) -> tuple[Any, Any, int]:
        """Dynamically configures LightGBM models to prevent finite-sample bias.

        In Double ML, overfitting the nuisance models breaks Neyman Orthogonality.
        We strictly constrain tree complexity on smaller datasets and adjust
        cross-fitting folds.
        """
        cv_folds = 5 if n_samples < 5000 else 3

        if n_samples < 500:
            max_depth = 2
            n_estimators = 50
        elif n_samples < 5000:
            max_depth = 3
            n_estimators = 100
        elif n_samples < 50000:
            max_depth = 5
            n_estimators = 100
        else:
            max_depth = 7
            n_estimators = 150

        # Enforce minimum samples per leaf to prevent memorization of noise.
        min_child = int(np.clip(n_samples * 0.02, 20, 500))

        lgb_params = {
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "num_leaves": (2**max_depth) - 1,
            "min_child_samples": min_child,
            "learning_rate": 0.05,
            "n_jobs": -1,
            "verbose": -1,
        }

        model_y = lgb.LGBMRegressor(**lgb_params)

        if self.discrete_treatment:
            model_t = lgb.LGBMClassifier(**lgb_params)
        else:
            model_t = lgb.LGBMRegressor(**lgb_params)

        return model_y, model_t, cv_folds

    def _unpack_embeddings(
        self, df: pd.DataFrame, col_name: str
    ) -> tuple[pd.DataFrame, list[str]]:
        """Flattens a column of iterables into separate scalar columns."""
        df_work = df.copy()

        emb_matrix = np.stack(df_work[col_name].values)
        d_emb = emb_matrix.shape[1]

        feature_names = [f"{col_name}_{i}" for i in range(d_emb)]

        df_emb = pd.DataFrame(
            emb_matrix, columns=feature_names, index=df_work.index
        )
        df_work = pd.concat([df_work, df_emb], axis=1)
        df_work = df_work.drop(columns=[col_name])

        return df_work, feature_names
