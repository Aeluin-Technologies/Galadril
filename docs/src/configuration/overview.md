# Pipeline Configuration

Docker Compose mounts `infrastructure/docker/connectors.yaml`. Kubernetes and
k3s use `infrastructure/kubernetes/connectors.yaml`, with Service DNS and mesh
transport. Deployed services receive an explicit connector path; their fallback
is `/etc/galadril/connectors.yaml`, independent of the source checkout.
The standalone DAG in `infrastructure/configuration/pipeline.yaml` is a test
configuration. Production tenant
pipelines are authored and published through Gateway to Registry.

See [Pipeline storage and revision control](pipeline_storage.md) for defaults,
production deployment, tenant isolation, and migration from S3.
