# Galadril Vision

AI pipeline for predicting and linking (ontology) data.
This is the true Mirror of Galadriel.

> *"Many things I can command the Mirror to reveal,’ she answered. ‘And to some
> I can show what they desire to see. But the Mirror will also show things
> unbidden, and those are often stranger and more profitable than things which
> we wish to behold. What you will see, if you leave the Mirror free to work, I
> cannot tell. For it shows things that were, and things that are, and things
> that yet may be. But which it is that he sees, even the wisest cannot always
> tell."*

## Architecture

Galadril Vision uses a distributed, multi-tenant orchestration architecture for
multi-modal inference, entity resolution, and causal analysis. The stack
bridges real-time stream consumption with distributed batch query capabilities
using three core framework pillars:
1. **Dagster:** Handles lifecycle orchestration, tracking, and scheduling.
2. **Daft:** Performs parallel data processing and lazy dataframe
    transformation.
3. **Ray:** Serves as the distributed backing cluster for Daft's execution
    layer (`daft.set_runner_ray`). It handles resource isolation for model
    workloads via designated inference pools.

```mermaid
graph TD
    subgraph Ingestion [Ingestion Worker Layer]
        K[Kafka Stream] -->|Poll Batch| VP[VisionPipeline Runner]
        VP -->|Validate Payloads| VAL{Is Valid?}
        VAL -->|No| DLQ[Kafka Dead-Letter Queue]
        VAL -->|Yes| PART[Partition by Tenant & Topic]
        PART -->|Upload Parquet| S3T[(S3 Transit Store)]
    end

    VP -->|GraphQL Mutation| DS[Dagster Webserver / Daemon]
    DS -->|Trigger Asset Job| DA[vision_pipeline_batch Asset]

    subgraph Compute [Distributed Ray Cluster Engine]
        DA -->|Instantiate| EX[ESKGPipelineExecutor]
        S3T -->|daft.read_parquet| EX
        
        subgraph Daft [Daft Lazy Dataframe Graph]
            P1[Phase 1: Ingest & Download] -->|DownloadDataWorker| MC[Materialize .collect Checkpoint]
            MC --> P2[Phase 2: Transform & Resolve]
            P2 -->|run_inference_udf| INF[ML Model Embeddings]
            INF -->|resolve_entities_udf| ER[Entity Resolution Clustering]
            ER --> P3[Phase 3: Sink & Causal Analytics]
            P3 -->|sink_to_db_udf| CC[Final .collect Compute Execution]
        end
        EX --> Daft
        S3M[(S3 Models Bucket)] -.->|Fetch Artifacts| INF
    end

    subgraph Storage [Database & Analytics Architecture]
        CC -->|Batch Graph Edges| GSTR[(Apache AGE)]
        CC -->|Vector Embeddings| VSTR[(pgvector)]
        CC -->|Trigger Causal Engine| AC[Amarth Causal Runner]
        GSTR -.->|Evaluate Lookback Window| AC
        AC -->|Persist Inferred Causal Edges| GSTR
    end

    style Ingestion fill:#f9f,stroke:#333,stroke-width:2px
    style Compute fill:#bbf,stroke:#333,stroke-width:2px
    style Storage fill:#bfb,stroke:#333,stroke-width:2px
    style DS fill:#fbb,stroke:#333,stroke-width:2px
```
