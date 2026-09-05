# Vision Service

Vision Service is the brain of the platform, located in `platform/vision/`.
It is dynamically orchestrated by the `galadril-pipeline` library.
It relies on the `galadril-inference` library to standardize calls to ML
algorithms.

TODO: explain how to extend `galadril-inference` and `galadril-vision`.

## DAG Construction

Service loads credentials from `connectors.yaml` and builds DAGs for every
published TerminusDB revision in its trusted tenant capability map. One process
keeps tenant-specific route and ontology contexts while sharing Kafka consumers
and Ray CPU, GPU, and causal actors. An explicitly selected
`pipeline.example.yaml` remains available for local examples.
See [pipeline storage](../configuration/pipeline_storage.md) for deployment and
revision activation.
* It validates that no circular dependencies exist.
* It calculates the exact topological order required to execute models.

## Dynamic Model Loading
Models are not hardcoded. Service uses Python's `importlib` to instantiate the
exact classes defined in the configuration.
Model weights are automatically pulled from S3 into memory before processing
begins.

## Message Routing Loop

1. The service polls batches of messages from Kafka.
2. It validates the immutable tenant, pipeline, and revision Kafka headers and
   ensures the payload tenant agrees with the trusted header.
3. It selects exactly one published DAG by `(tenant, pipeline, revision)` and
   resolves the configured source within that DAG.
4. It passes the data to the model through a shared Ray resource pool.
5. After every command, including failures, the Ray actor clears request
   context variables, runs Python garbage collection, and clears the CUDA cache
   for GPU commands. LI-ESKG retains only its internally tenant-isolated runtime.
6. Once the model outputs a prediction, the service asks the selected DAG which
   step consumes the output next.
7. If no step needs it, the data has reached the end of the pipeline (a Sink).
