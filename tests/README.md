# Galadril tests 🧪

## Run

```bash
bazel test //tests/...
```

## Tests

| Target | Coverage |
| --- | --- |
| `//tests/e2e:e2e_support_test` | E2E orchestration contracts. |
| `//tests/e2e:pipeline_lifecycle_test` | Gateway-to-Gateway data lifecycle. |

## E2E data flow

```mermaid
flowchart LR
    Tenant["Tenant"] -->|"request upload"| GatewayUpload["Gateway upload API"]
    GatewayUpload -->|"presigned upload"| Staging[("S3 staging")]
    Tenant -->|"complete upload"| GatewayUpload
    GatewayUpload -->|"promote"| Raw[("S3 tenant/raw")]
    Raw -->|"object event"| Intake["Intake"]
    Intake -->|"trusted context"| Kafka[("Kafka")]
    Registry[("Registry + lakeFS")] -->|"ontology + pipeline revision"| Vision["Vision DAG"]
    Kafka --> Vision
    Vision -->|"entity state"| PostgreSQL[("PostgreSQL")]
    Vision -->|"lineage permissions"| SpiceDB[("SpiceDB")]
    PostgreSQL --> GatewayAccess["Gateway access API"]
    SpiceDB -->|"authorize"| GatewayAccess
    GatewayAccess -->|"authorized result"| Tenant

    GatewayUpload -.->|"trace"| Tempo[("Tempo")]
    Intake -.->|"trace"| Tempo
    Vision -.->|"trace"| Tempo
```
