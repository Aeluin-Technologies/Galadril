"""Pure runtime DAG fixture shared by contract and interservice tests."""


def pipeline_fixture(tenant_id: str, pipeline_id: str) -> dict[str, object]:
    """Returns one deterministic source-to-sink runtime DAG."""
    return {
        "version": 1,
        "name": pipeline_id,
        "sources": [
            {
                "id": "e2e_text",
                "topic": "e2e.raw",
                "match_pattern": (
                    rf"^{tenant_id}/raw/default/(?:[a-f0-9]{{32}}/)?e2e-record(?:-[0-9]{{2}})?\.txt$"
                ),
                "schema_path": "schemas/avro/text.avsc",
                "parser": "text",
                "source_kind": "e2e_fixture",
            },
            {
                "id": "e2e_empirical",
                "topic": "e2e.raw",
                "match_pattern": (
                    rf"^{tenant_id}/raw/(?:sales/(?:[a-f0-9]{{32}}/)?sales-[0-9]{{2}}|returns/(?:[a-f0-9]{{32}}/)?returns-[0-9]{{2}}|trial/(?:[a-f0-9]{{32}}/)?trial-205)\.csv$"
                ),
                "schema_path": "schemas/avro/text.avsc",
                "parser": "csv",
                "source_kind": "e2e_empirical",
            },
        ],
        "pipeline": [
            {
                "step": "infer",
                "type": "inference",
                "input_from": ["e2e_text"],
                "model": "e2e_deterministic",
                "artifact_path": "/tmp/galadril-e2e-model",
                "params": {
                    "action": "embed",
                    "gol_transfer_symbol_key": "e2e.lifecycle::E2EEntity",
                },
            },
            {
                "step": "resolve",
                "type": "resolve",
                "input_from": ["infer"],
                "params": {
                    "gol_state_symbol_key": "e2e.lifecycle::IdentityPrototype",
                    "gol_transfer_symbol_key": "e2e.lifecycle::E2EEntity",
                    "modality": "e2e",
                    "threshold": 0.85,
                    "entity_type": "E2E_ENTITY",
                },
            },
            {
                "step": "sink",
                "type": "sink",
                "connector": "postgres",
                "input_from": ["resolve"],
                "params": {
                    "gol_transfer_symbol_key": "e2e.lifecycle::E2EEntity",
                    "entity_type": "E2E_ENTITY",
                    "modality": "e2e",
                    "state_type": "E2E_OBSERVATION",
                    "gol_symbol_key": "e2e.lifecycle::E2EEntity",
                    "gol_event_symbol_key": "e2e.lifecycle::E2EObservation",
                },
            },
            {
                "step": "causal",
                "type": "causal",
                "input_from": [],
                "params": {
                    "gol_transfer_symbol_key": "e2e.lifecycle::E2EObservation",
                    "trigger": "on_demand",
                    "amarth_target_outcome": "E2E_OBSERVATION.confidence",
                    "amarth_window_size": "120s",
                    "lookback": "120s",
                    "bucket": "1s",
                    "max_events": 64,
                    "max_states": 64,
                    "k_min": 1,
                    "k_max": 1,
                    "max_vertices": 8,
                    "include_presence_links": False,
                },
            },
            {
                "step": "empirical_infer",
                "type": "inference",
                "input_from": ["e2e_empirical"],
                "model": "e2e_deterministic",
                "artifact_path": "/tmp/galadril-e2e-model",
                "params": {
                    "action": "embed",
                    "gol_transfer_symbol_key": "e2e.lifecycle::E2EEntity",
                },
            },
            {
                "step": "empirical_resolve",
                "type": "resolve",
                "input_from": ["empirical_infer"],
                "params": {
                    "gol_state_symbol_key": "e2e.lifecycle::IdentityPrototype",
                    "gol_transfer_symbol_key": "e2e.lifecycle::E2EEntity",
                    "modality": "e2e",
                    "threshold": 0.85,
                    "entity_type": "E2E_ENTITY",
                },
            },
            {
                "step": "empirical_sink",
                "type": "sink",
                "connector": "postgres",
                "input_from": ["empirical_resolve"],
                "params": {
                    "gol_transfer_symbol_key": "e2e.lifecycle::E2EEntity",
                    "entity_type": "E2E_ENTITY",
                    "modality": "e2e",
                    "state_type": "E2E_OBSERVATION",
                    "gol_symbol_key": "e2e.lifecycle::E2EEntity",
                    "gol_event_symbol_key": "e2e.lifecycle::E2EObservation",
                },
            },
        ],
    }
