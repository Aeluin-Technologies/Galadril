# Model runtime benchmarks

The benchmark command keeps artifact download, model loading, input decoding,
and request construction outside the timed prediction loop. It performs warm-up
calls before collecting raw steady-state samples and reports p50/p95/p99 latency,
throughput, peak RSS, provider selection, and model metadata.

For Whisper, diarization is enabled by default so the benchmark exercises both
CTranslate2 transcription and speaker embedding. The report also includes audio
real-time factor; values below `1.0` process audio faster than real time. Use the
focused target below to measure the pyannote segmentation graph itself; the
application currently loads that graph but does not call it from `predict()`.

```bash
# CPU baseline from the baseline revision/worktree.
bazel run //machine-learning/galadril-inference/benchmarks:model_runtime -- \
  run --model whisper \
  --artifact-path /absolute/path/to/whisper-artifacts \
  --input machine-learning/galadril-inference/examples/audio/galadriel.mp3 \
  --device cpu --compute-type int8 --iterations 20 --label baseline \
  --output /tmp/whisper-baseline.json

# A historical model file can also be tested without changing the worktree:
# add --implementation-file /tmp/whisper-baseline.py after exporting it with
# git show HEAD:path/to/whisper.py > /tmp/whisper-baseline.py

# Candidate run from this revision on the same device and input.
bazel run //machine-learning/galadril-inference/benchmarks:model_runtime -- \
  run --model whisper \
  --artifact-path /absolute/path/to/whisper-artifacts \
  --input machine-learning/galadril-inference/examples/audio/galadriel.mp3 \
  --device cpu --compute-type int8 --iterations 20 --label onnx-candidate \
  --output /tmp/whisper-candidate.json

# Fail with exit code 2 if median latency improved by less than 10%.
bazel run //machine-learning/galadril-inference/benchmarks:model_runtime -- \
  compare --baseline /tmp/whisper-baseline.json \
  --candidate /tmp/whisper-candidate.json \
  --require-latency-improvement-pct 10
```

The same runner supports `siglip2`, `owlv2`, `grounded_sam`, and `timesfm`.
Pass `--download` on the first run to materialize the selected precision. Avoid
mixing machines, inputs, precision, provider versions, or thermal states when
comparing reports.

Run a separate `--device auto` candidate when the goal is specifically to
measure accelerator-provider gains rather than the framework change itself.

```bash
# Compare the exact historical session constructor with the shared optimized one.
bazel run //machine-learning/galadril-inference/benchmarks:whisper_segmentation_runtime -- \
  --model-path /absolute/path/to/diarization/onnx/model_int8.onnx \
  --runtime legacy --device cpu --output /tmp/whisper-segmentation-legacy.json

bazel run //machine-learning/galadril-inference/benchmarks:whisper_segmentation_runtime -- \
  --model-path /absolute/path/to/diarization/onnx/model_int8.onnx \
  --runtime optimized --device cpu --output /tmp/whisper-segmentation-optimized.json
```
