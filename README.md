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

### SOTA Engine: ESKG-enhanced GraphRAG

Galadril implements a reasoning framework based on the Event-State Knowledge
Graph (ESKG), as described in [Zang et al. (2026)](https://doi.org/10.1016/j.eswa.2026.131938).

Galadril represents the system as an evolving heterogeneous graph
$G_t = (V_t, R_t)$, where:
* $V_t = \{S \cup E\}$ is the set of vertices comprising **States** ($S$) and
    **Events** ($E$).
* $R_t \subseteq \{V_t \times \mathcal{T} \times V_t\}$ is the set of relations,
    where $\mathcal{T}$ represents the six fundamental interaction types:
    * **Triggers** ($E \xrightarrow{trig} S$): An event directly initiating a
        new state.
    * **Leads to** ($E_i \xrightarrow{lead} E_j$): A logical or temporal
        sequence between two events.
    * **Evolution** ($S_i \xrightarrow{evol} S_j$): A natural transition or
        progression between two states.
    * **Contain** ($E \supset S$ or $S \supset E$): A hierarchical inclusion of
        an event within a state (or vice-versa).
    * **Occur** ($E \xrightarrow{occ} L, T$): Spatio-temporal anchoring of an
        event to a location and time.
    * **Influence** ($E \xrightarrow{infl} P$): An event modifying a numerical
        property or parameter of an entity.

The core of the ESKG is the triggering mechanism that governs graph evolution.
When an event $E_i$ occurs, it satisfies a transition function $f$ that maps the
previous state to a new one: $$f: (S_{old}, E_i) \rightarrow S_{new}$$

This implies that for every state update in the "Mirror", Galadril enforces a
causal constraint: $$\exists E \in V_t \mid (E, \text{trig}, S_{new}) \in R_t$$

## License

This project is licensed under the terms of the BSD 3-Clause License*. See the
LICENSE file for the full license text.
