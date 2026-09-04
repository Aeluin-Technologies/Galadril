# Introduction to Galadril

Welcome to the technical documentation for **Galadril**.

Galadril is a scalable, configuration-driven platform designed to ingest
heterogeneous data, such as documents, financial logs, and OSINT, and process
it through high-performance, custom machine learning pipelines.

## Core Philosophy

* **Configuration-Driven**: Orchestrate your entire system via a single
    `connectors.yaml` service settings and versioned pipeline definitions for reproducibility.
* **Event-Driven Architecture**: High-throughput communication between
    components is handled natively via Apache Kafka.

## Prerequisites

To effectively build with Galadril, you should be familiar with the following
core technologies:

| Technology | Purpose in Galadril |
| :--- | :--- |
| **YAML** | Pipeline config for services. |
| **Python** | Custom ML models (JAX, PyTorch, etc.) via galadril-vision. |
| **Avro** | Data schemas and service interoperability. |

> **TIP:**
> Since these are industry-standard languages, LLMs have excellent support for
> them. If you run into schema issues or need boilerplate code, use them to
> speed up your workflow.

## License

Source code and documentation are released under the 
[MPL-2.0](https://opensource.org/license/MPL-2.0) license.