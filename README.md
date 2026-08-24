# Galadril ⛲️

[Documentation](https://realhinome.github.io/Galadril/) | 
[GitHub](https://github.com/RealHinome/Galadril)

> *"Things that were, and things that are, and some things that have not yet
> come to pass."*

**Galadril** is an advanced data integration and analytical intelligence
platform designed to provide a "Mirror" of complex systems. Galadril focuses
on **elucidation, foresight, and transparency**.

> [!CAUTION]
> This project is still in its early stages.

## Development
Enter the shell to load the environment:
```bash
nix develop github:RealHinome/Galadril?dir=infrastructure/nix
```

## Targeted architecture

```mermaid
flowchart TD
    classDef source fill:#e3f2fd,stroke:#1565c0,stroke-width:2px,color:#0d47a1
    classDef ingest fill:#ffecb3,stroke:#ff6f00,stroke-width:2px,color:#3e2723
    classDef stream fill:#d1c4e9,stroke:#512da8,stroke-width:2px,color:#311b92
    classDef ml fill:#f8bbd0,stroke:#c2185b,stroke-width:2px,color:#880e4f
    classDef pg fill:#336791,stroke:#000,stroke-width:2px,color:#fff
    classDef app fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px,color:#1b5e20
    classDef bus fill:#212121,stroke:#fff,stroke-width:2px,color:#fff,stroke-dasharray: 5 5

    subgraph Sources ["Multi-Modal Data Sources"]
        direction TB
        S1[("Sensors / IoT / SIGINT")]:::source
        S2[("Financial / ERP Flows")]:::source
        S3[("Unstructured (OSINT/Docs)")]:::source
        S4[("3rd Party APIs")]:::source
    end

    subgraph Ingestion ["Ingestor"]
        Connectors["Smart Connectors"]:::ingest
        Raw_Bus[("Raw Event Bus (Kafka)")]:::bus
    end

    subgraph Processing ["The Vision"]
        Stream_Engine["Stream Processor"]:::stream

        subgraph Compute ["Compute Services"]
            Entity_Res["Entity Resolution"]:::ml
            Ontology_Map["Ontology Mapper"]:::stream
            ML_Inf["ML Inference"]:::ml
        end

        Feature_Store["Feature Store (Online)"]:::pg
    end

    subgraph Knowledge ["The Synapse"]
        Intel_Bus[("Curated Intel Bus (Kafka)")]:::bus

        subgraph PG_Engine ["PostgreSQL"]
            direction TB
            KG[("Apache AGE")]:::pg
            VecDB[("pgvectorscale")]:::pg
            Relational[("Relational")]:::pg
            Timescale[("TimescaleDB")]:::pg
        end

        ObjStore[("Object Store")]:::pg
    end

    subgraph Consumption ["Galadril Studio"]
        Gateway["Unified Ontology API"]:::app
        Studio["Investigation Graphs"]:::app
        Alerts["Operational Alerting"]:::app
    end

    S1 & S2 & S3 & S4 --> Connectors
    Connectors --> Raw_Bus
    Connectors -->|Direct Backup| ObjStore

    Raw_Bus --> Stream_Engine

    Stream_Engine <--> Ontology_Map
    Stream_Engine <--> Feature_Store

    Feature_Store -.-> |"Get Features"| ML_Inf
    ML_Inf --> Stream_Engine

    Entity_Res <--> |"Lookup / Match"| Relational
    Stream_Engine <--> Entity_Res

    Stream_Engine --> Intel_Bus
    Intel_Bus --> PG_Engine

    PG_Engine & ObjStore --> Gateway
    Gateway <--> Studio
    Gateway --> Alerts
```

### ESKG-enhanced GraphRAG

Galadril implements a reasoning framework based on the Event-State Knowledge
Graph (ESKG), as described in
[Zang et al. (2026)](https://doi.org/10.1016/j.eswa.2026.131938).

Its core transition pattern is:

$$
S_t \xrightarrow{Triggers} E_t \xrightarrow{LeadsTo} S_{t+1}
$$

A state enables an event, the event produces a new state, and the graph grows
with this history. This makes state changes traceable rather than simply
overwriting the previous value.

The base ESKG defines six relations:

| Relation                      | Meaning                                                      |
| ----------------------------- | ------------------------------------------------------------ |
| `Triggers` ($S \rightarrow E$)  | A state enables or activates an event.                       |
| `LeadsTo` ($E \rightarrow S$)   | An event produces a resulting state.                         |
| `Evolution` ($S \rightarrow S$) | A state directly evolves into another state.                 |
| `Contain` ($E \rightarrow E$)   | An event contains or decomposes into other events.           |
| `Occur` ($E \rightarrow P$)     | An event occurs at or is associated with a physical entity.  |
| `Influence` ($E \rightarrow P$) | An event affects a physical entity or one of its properties. |

Galadril extends this model with Latent Identity ESKG. It therefore keeps
evidence and identity hypotheses separate from authoritative graph facts until
a resolution decision is made.

Its additional relations describe that resolution process:

| Relation                 | Meaning                                                                     |
| ------------------------ | --------------------------------------------------------------------------- |
| `hasInference`           | Links an observation to its inference result.                               |
| `consideredCandidate`    | Records identities considered during inference.                             |
| `hasDecision`            | Links an inference to the resulting decision.                               |
| `selectedTarget`         | Records the identity selected by that decision.                             |
| `promotedAs`             | Resolves a latent identity into an authoritative entity.                    |
| `mergedWith`             | Versionably merges two latent identity hypotheses.                          |
| `supersedes` / `revokes` | Replaces or invalidates an earlier interpretation without deleting history. |

Amarth adds a derived `CAUSES` relationship after time-windowed causal
inference. Unlike the base semantic `Influence` relation, `CAUSES` carries
quantitative evidence: FDR-corrected confidence, effect size, lag, window
bounds, method, and source observation provenance. Relationships are versioned
by inference window and can seed DoWhy structural causal models for
intervention and counterfactual simulation.

## License

This project is licensed under the terms of the BSD 3-Clause License. See the
[LICENSE](/LICENSE) file for the full license text.
