## Galadril-inference examples.

These examples are not layered. Each one illustrates the use of a single model.

## Runtime installation

Install only the backends required by the deployment. ONNX is the default for
embeddings, object detection, face recognition, time-series forecasting, and
audio diarization:

```bash
# Portable CPU deployment.
uv sync --extra onnx-cpu --extra embeddings --extra vision

# NVIDIA GPU deployment (do not install the CPU ONNX extra as well).
uv sync --extra onnx-gpu --extra embeddings --extra vision
```

ONNX execution providers are selected automatically in accelerator-first order.
Set `GALADRIL_DEVICE=cpu`, `cuda`, `rocm`, `directml`, `coreml`, or `openvino`
to pin a backend. `GALADRIL_ONNX_INTRA_OP_THREADS` and
`GALADRIL_ONNX_INTER_OP_THREADS` cap ONNX worker threads for latency-sensitive
services. Install `legacy-torch` only for models that do not yet have a
reliable ONNX equivalent, such as GLM-OCR and GeoCLIP.
