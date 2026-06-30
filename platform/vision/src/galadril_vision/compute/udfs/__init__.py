"""Initialization of the Daft UDFs module exposing compute workers."""

from galadril_vision.compute.udfs.download import DownloadDataWorker
from galadril_vision.compute.udfs.inference import run_inference_udf
from galadril_vision.compute.udfs.resolve import resolve_entities_udf
from galadril_vision.compute.udfs.sink import sink_to_db_udf

__all__ = [
    "DownloadDataWorker",
    "run_inference_udf",
    "resolve_entities_udf",
    "sink_to_db_udf",
]
