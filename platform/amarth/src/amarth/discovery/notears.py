"""Custom implementation of the NOTEARS algorithm for continuous DAG discovery."""

import numpy as np
import scipy.linalg as slin
import scipy.optimize as sopt
import networkx as nx
import pandas as pd


class NotearsDiscoverer:
    """Discovers a causal DAG using continuous optimization (NOTEARS)."""

    def __init__(self, threshold: float = 0.3, max_iter: int = 100):
        """Initializes the NOTEARS discoverer.

        Args:
            threshold: Minimum absolute weight to consider an edge valid.
            max_iter: Maximum iterations for the Augmented Lagrangian.
        """
        self.threshold = threshold
        self.max_iter = max_iter

    def fit(self, df: pd.DataFrame) -> nx.DiGraph:
        """Fits the NOTEARS algorithm to the dataset.

        Args:
            df: Input data where columns are variables.

        Returns:
            A directed acyclic graph (NetworkX) representing causal relations.
        """
        X = df.to_numpy()
        n, d = X.shape

        # Initialize weight matrix W (flattened for L-BFGS-B).
        w_est, _ = self._solve_augmented_lagrangian(X, n, d)

        # Thresholding to prune weak correlations and enforce sparsity.
        W = w_est.reshape(d, d)
        W[np.abs(W) < self.threshold] = 0

        dag = nx.DiGraph()
        dag.add_nodes_from(df.columns)

        for i in range(d):
            for j in range(d):
                if W[i, j] != 0:
                    dag.add_edge(df.columns[i], df.columns[j], weight=W[i, j])

        return dag

    def _solve_augmented_lagrangian(
        self, X: np.ndarray, n: int, d: int
    ) -> tuple[np.ndarray, dict]:
        """Solves the constrained optimization using Augmented Lagrangian."""
        rho, alpha, h = 1.0, 0.0, np.inf
        w_est = np.zeros(d * d)

        # Bounds: prevent self-loops (diagonal W_ii = 0).
        bnds = [
            (0, 0) if i == j else (None, None)
            for i in range(d)
            for j in range(d)
        ]

        for _ in range(self.max_iter):
            w_new, h_new = None, None

            while rho < 1e20:
                res = sopt.minimize(
                    fun=self._dual_obj,
                    x0=w_est,
                    args=(X, n, d, rho, alpha),
                    method="L-BFGS-B",
                    jac=True,
                    bounds=bnds,
                )
                w_new = res.x
                h_new, _ = self._h_val_and_grad(w_new, d)
                if h_new > 0.25 * h:
                    rho *= 10
                else:
                    break

            w_est, h = w_new, h_new
            alpha += rho * h
            if h <= 1e-8:
                break

        return w_est, {}

    def _dual_obj(
        self,
        w: np.ndarray,
        X: np.ndarray,
        n: int,
        d: int,
        rho: float,
        alpha: float,
    ) -> tuple[float, np.ndarray]:
        """Computes the Augmented Lagrangian objective and gradient."""
        W = w.reshape(d, d)

        # Least squares loss and gradient.
        R = X - X @ W
        loss = 0.5 / n * np.square(R).sum()
        G_loss = -1.0 / n * X.T @ R

        # Acyclicity constraint and gradient.
        h, G_h = self._h_val_and_grad(W, d)

        obj = loss + 0.5 * rho * h * h + alpha * h
        grad = G_loss + (rho * h + alpha) * G_h

        return obj, grad.flatten()

    def _h_val_and_grad(
        self, W: np.ndarray, d: int
    ) -> tuple[float, np.ndarray]:
        """Computes the acyclicity constraint h(W) and its gradient."""
        E = slin.expm(W * W)
        h = np.trace(E) - d
        G_h = E.T * 2 * W
        return h, G_h
