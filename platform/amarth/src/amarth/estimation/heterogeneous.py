"""Heterogeneous treatment effect estimation using DoWhy and EconML.

Uses Double Machine Learning (DML) with LightGBM to handle high-dimensional
embeddings (e.g., pgvector) as confounders while avoiding statistical bias.
"""

import warnings
from dataclasses import dataclass
from time import perf_counter

import lightgbm as lgb
import networkx as nx
import numpy as np
import pandas as pd
import structlog
from dowhy import CausalModel
from sklearn.decomposition import PCA
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

    __slots__ = (
        "discrete_treatment",
        "max_embedding_components",
        "n_jobs",
        "refutation_simulations",
    )

    def __init__(
        self,
        discrete_treatment: bool = False,
        *,
        max_embedding_components: int = 16,
        refutation_simulations: int = 5,
        n_jobs: int = 1,
    ) -> None:
        """Initializes the HTE estimator.

        Args:
            discrete_treatment: True if the treatment is categorical/boolean.
            max_embedding_components: Maximum PCA dimensions passed to DML.
            refutation_simulations: Bounded DoWhy subset refits; zero disables.
            n_jobs: Thread limit for LightGBM and DoWhy refutation.

        Raises:
            ValueError: If a resource bound is invalid.
        """
        if max_embedding_components < 1:
            raise ValueError("max_embedding_components must be positive")
        if refutation_simulations < 0:
            raise ValueError("refutation_simulations must be non-negative")
        if n_jobs < 1:
            raise ValueError("n_jobs must be positive")
        self.discrete_treatment = discrete_treatment
        self.max_embedding_components = max_embedding_components
        self.refutation_simulations = refutation_simulations
        self.n_jobs = n_jobs

    def estimate_effect(
        self,
        df: pd.DataFrame,
        treatment: str,
        outcome: str,
        embedding_col: str,
        dag: nx.DiGraph | None = None,
    ) -> HeterogeneousEstimateResult | None:
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

        fit_started = perf_counter()
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
                    "random_state": 0,
                    "use_ray": False,
                },
                "fit_params": {
                    "inference": "auto",
                },
            },
        )
        logger.info(
            "dml_model_fitted",
            elapsed_seconds=round(perf_counter() - fit_started, 3),
            samples=n_samples,
            dimensions=len(emb_feature_names),
        )

        effect_modifiers = df_processed.loc[:, emb_feature_names]
        cates = np.asarray(
            estimate.estimator.effect(effect_modifiers), dtype=np.float64
        )
        ate = np.mean(cates)
        cate_std = np.std(cates)

        p_value = None
        try:
            inference = estimate.estimator.estimator.ate_inference(
                effect_modifiers.to_numpy(dtype=np.float64, copy=False)
            )
            raw_p = np.asarray(inference.pvalue(), dtype=np.float64).reshape(-1)
            if raw_p.size > 0 and np.isfinite(raw_p[0]):
                p_value = float(np.clip(raw_p[0], 0.0, 1.0))
        except Exception as e:
            logger.warning("failed_to_extract_p_value", error=str(e))

        refutation_passed = False
        if self.refutation_simulations > 0:
            refute_started = perf_counter()
            refute = model.refute_estimate(
                identified_estimand,
                estimate,
                method_name="data_subset_refuter",
                subset_fraction=0.8,
                num_simulations=self.refutation_simulations,
                n_jobs=self.n_jobs,
            )
            refutation_passed = refute.new_effect is not None
            logger.info(
                "dml_refutation_completed",
                elapsed_seconds=round(perf_counter() - refute_started, 3),
                simulations=self.refutation_simulations,
            )

        return HeterogeneousEstimateResult(
            treatment=treatment,
            outcome=outcome,
            ate=float(ate),
            cate_std=float(cate_std),
            refutation_passed=refutation_passed,
            summary=str(estimate),
            p_value=p_value,
        )

    def _build_nuisance_models(
        self, n_samples: int
    ) -> tuple[
        lgb.LGBMRegressor,
        lgb.LGBMRegressor | lgb.LGBMClassifier,
        int,
    ]:
        """Dynamically configures LightGBM models to prevent finite-sample bias.

        In Double ML, overfitting the nuisance models breaks Neyman Orthogonality.
        We strictly constrain tree complexity on smaller datasets and adjust
        cross-fitting folds.
        """
        cv_folds = 2 if n_samples < 5000 else 3

        if n_samples < 500:
            max_depth = 2
            n_estimators = 24
        elif n_samples < 5000:
            max_depth = 3
            n_estimators = 32
        elif n_samples < 50000:
            max_depth = 4
            n_estimators = 64
        else:
            max_depth = 5
            n_estimators = 96

        # Enforce minimum samples per leaf to prevent memorization of noise.
        min_child = int(np.clip(n_samples * 0.02, 20, 500))

        lgb_params = {
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "num_leaves": (2**max_depth) - 1,
            "min_child_samples": min_child,
            "learning_rate": 0.05,
            "n_jobs": self.n_jobs,
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

        emb_matrix = np.stack(df_work[col_name].values).astype(
            np.float64, copy=False
        )
        dimension = emb_matrix.shape[1]
        retained_dimensions = min(
            dimension,
            self.max_embedding_components,
            len(emb_matrix),
        )
        if retained_dimensions < dimension:
            emb_matrix = PCA(
                n_components=retained_dimensions,
                svd_solver="randomized",
                random_state=0,
                copy=False,
            ).fit_transform(emb_matrix)

        feature_names = [
            f"{col_name}_{index}" for index in range(retained_dimensions)
        ]

        df_emb = pd.DataFrame(
            emb_matrix, columns=feature_names, index=df_work.index
        )
        df_work = pd.concat([df_work, df_emb], axis=1)
        df_work = df_work.drop(columns=[col_name])

        return df_work, feature_names
