# Eru -- ESKG Extraction

`eru` is a production-ready, agnostic pipeline designed to extract highly
structured Knowledge Graphs (Nodes and Edges) from unstructured text. 

## Why does it exist?

Extracting knowledge graphs using LLMs alone is flawed: they hallucinate
entities, fail to strictly follow JSON schemas, and consume massive amounts of
tokens (which is slow and expensive). Conversely, traditional NER (Named Entity
Recognition) models extract entities perfectly but cannot understand complex
relationships or implicit intents.

Eru bridges this gap. It forces small, fast LLMs to act *only* as logical
routers between physically extracted entities, guaranteeing deterministic,
hallucination-free graph extraction.

## The 3-Layer Architecture

Eru processes text through a strict, three-step pipeline:

| Layer | Description |
|---|---|
| **Extraction** | Uses a Bi-encoder (GLiNER) to find physical entities in the text (e.g., Persons, Locations). It automatically deduplicates identical entities. |
| **Reasoning** | Uses a SLM constrained by `outlines`. The LLM is forbidden from inventing physical entities; it can **only** draw relationships using the IDs from Layer 1, or generate implicit conceptual nodes. |
| **Validation** | A logical gate that prunes mathematically impossible relationships (e.g., a "Car" cannot "Authorize" a "Person") before the graph is saved to your database. |

---

Here is a minimal example of how to use Eru to extract a graph from a simple
sentence, allowing the model to deduce the implicit *intent* behind the action.

```python
import json
from typing import Literal
from pydantic import BaseModel, Field
from transformers import AutoModelForCausalLM, AutoTokenizer
import outlines

from eru.engine import EskgEngine
from eru.extractor.gliner import GlinerExtractor
from eru.reasoner.outlines import OutlinesReasoner
from eru.logic.eskg import EskgLogicValidator
from eru.types import RelationDef


# 1. Define your Graph Schema
class Node(BaseModel):
    id: str
    text: str
    type: str


class Edge(BaseModel):
    source_id: str
    target_id: str
    relation_type: Literal["buys", "has_intent"]


class DailyGraph(BaseModel):
    entities: list[Node]
    relations: list[Edge]


def main():
    text = "Alice purchased a Macbook today because she wants to learn coding."

    # Layer 1: Extract explicit physical entities
    extractor = GlinerExtractor(
        labels=["PERSON", "PRODUCT", "TIME"],
        threshold=0.3,
    )

    # Define relationship rules for the LLM
    rules = [
        RelationDef(
            name="buys",
            description="When a person purchases an item.",
            allowed_sources=["PERSON"],
            allowed_targets=["PRODUCT"],
        ),
        RelationDef(
            name="has_intent",
            description="The implicit reason or goal behind the action.",
        ),
    ]

    # Layer 2: Setup the SLM Reasoner
    model_name = "Qwen/Qwen2.5-0.5B-Instruct"
    hf_model = AutoModelForCausalLM.from_pretrained(
        model_name, device_map="auto"
    )
    hf_tokenizer = AutoTokenizer.from_pretrained(model_name)
    llm = outlines.from_transformers(hf_model, hf_tokenizer)

    reasoner = OutlinesReasoner(
        model=llm,
        relation_defs=rules,
        open_entity_types=[
            "INTENT"
        ],  # Allow LLM to invent concepts like 'learning to code'
    )

    # Layer 3: Setup the Logic Validator
    validator = EskgLogicValidator(
        get_entities=lambda g: g.entities, get_relations=lambda g: g.relations
    )

    # Run the Engine
    engine = EskgEngine(
        schema=DailyGraph,
        extractor=extractor,
        reasoner=reasoner,
        validator=validator,
    )
    graph = engine.process(text)

    print(json.dumps(graph.model_dump(), indent=2))


if __name__ == "__main__":
    main()
```

### Expected Output
The engine cleanly separates the physically extracted nodes (Alice, Macbook)
from the inferred conceptual node (learning to code).

```json
{
  "entities": [
    {"id": "ent_0", "text": "Alice", "type": "PERSON"},
    {"id": "ent_1", "text": "Macbook", "type": "PRODUCT"}
  ],
  "relations": [
    {
      "source_id": "ent_0",
      "target_id": "ent_1",
      "relation_type": "buys"
    },
    {
      "source_id": "ent_0",
      "target_id": "to learn coding",
      "relation_type": "has_intent"
    }
  ]
}
```
