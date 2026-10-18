"""Exact small-corpus CPU cosine KNN avoids importing Torch in differential tests."""

import numpy as np
from numpy.typing import NDArray


def retrieve_knn(
    query_ids: list[str],
    key_ids: list[str],
    query_vecs: NDArray[np.float32],
    key_vecs: NDArray[np.float32],
    k: int,
    query_batch_size: int,
    key_batch_size: int,
) -> dict[str, tuple[list[str], list[float]]]:
    if not key_ids:
        return {}
    if len(key_ids) > 1024 or len(query_ids) > 1024:
        raise ValueError("Reference KNN budget exceeded")
    queries = np.asarray(query_vecs, dtype=np.float32)
    keys = np.asarray(key_vecs, dtype=np.float32)
    queries = queries / np.linalg.norm(queries, axis=1, keepdims=True)
    keys = keys / np.linalg.norm(keys, axis=1, keepdims=True)
    scores = queries @ keys.T
    result: dict[str, tuple[list[str], list[float]]] = {}
    for identifier, values in zip(query_ids, scores, strict=True):
        order = np.argsort(-values, kind="stable")[: min(k, len(key_ids))]
        result[identifier] = (
            [key_ids[int(index)] for index in order],
            [float(values[index]) for index in order],
        )
    return result
