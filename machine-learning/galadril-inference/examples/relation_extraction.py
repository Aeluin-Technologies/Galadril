"""Inference script for the Eru extraction model."""

import logging
import sys
from pathlib import Path

import structlog
from galadril_inference.common.types import PredictionRequest
from galadril_inference.core.engine import InferenceEngine
from galadril_inference.loading.loader import ArtifactLoader

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True),
    ],
)

GALADRIL_ROOT = None
for parent in Path(__file__).resolve().parents:
    if parent.name == "Galadril":
        GALADRIL_ROOT = parent
        if str(parent) not in sys.path:
            sys.path.append(str(parent))
        break

if not GALADRIL_ROOT:
    print(
        "Error: Unable to locate the 'Galadril' project root directory.",
        file=sys.stderr,
    )
    sys.exit(1)

TEXT = (
    "On March 12, the airstrike destroyed the main bridge in Kyiv, "
    "causing the enemy army's logistics to collapse by 40%."
)


class EruLoader(ArtifactLoader):
    def resolve(self, name: str, version: str) -> str:
        if name != "eru":
            return ""

        base_dir = (
            GALADRIL_ROOT / "machine-learning" / "models" / "eru_artifacts"
        )
        llm_dir = base_dir / "llm"
        gliner_dir = base_dir / "gliner2"

        llm_dir.mkdir(parents=True, exist_ok=True)
        gliner_dir.mkdir(parents=True, exist_ok=True)

        from huggingface_hub import hf_hub_download, snapshot_download

        hf_gguf_path = hf_hub_download(
            repo_id="bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF",
            filename="Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        )
        hf_gliner_path = snapshot_download(
            repo_id="knowledgator/gliner-bi-base-v2.0"
        )

        target_gguf = llm_dir / Path(hf_gguf_path).name
        if not target_gguf.exists() and not target_gguf.is_symlink():
            target_gguf.symlink_to(hf_gguf_path)

        if not gliner_dir.exists() and not gliner_dir.is_symlink():
            gliner_dir.symlink_to(hf_gliner_path, target_is_directory=True)

        return str(base_dir)

    def exists(self, name: str, version: str) -> bool:
        return name == "eru"

    def upload(self, model_name: str, version: str, local_path: str) -> None:
        pass


def print_results(model_name: str, prediction: dict, latency: float) -> None:
    print(f"\n{'=' * 20} RESULTS: {model_name.upper()} {'=' * 20}")
    print(f"Latency: {latency:.2f} ms")

    print("\n[ENTITIES]")
    entities = prediction.get("entities", [])
    if isinstance(entities, dict):
        for etype, values in entities.items():
            print(f"  - {etype}: {', '.join(values) if values else 'None'}")
    else:
        for ent in entities:
            name = ent.get("text", ent.get("name", "Unknown"))
            print(f"  - {ent.get('type')}: {name}")

    print("\n[RELATIONS]")
    relations = prediction.get("relations", [])
    if not relations:
        print("  No relations detected.")
    for rel in relations:
        source = rel.get("source_id", rel.get("source"))
        target = rel.get("target_id", rel.get("target"))
        print(f"  ({source}) --[{rel.get('relation_type')}]--> ({target})")


def main():
    loader = EruLoader()
    engine = InferenceEngine(loader=loader)
    model_id = "eru"

    try:
        engine.load_model(model_id)

        features = {"text": TEXT}
        req = PredictionRequest(model_name=model_id, features=features)

        result = engine.predict(req)
        print_results(model_id, result.prediction, result.latency_ms)

    except Exception as e:
        print(f"Error with {model_id}: {e}", file=sys.stderr)

    finally:
        try:
            engine.unload_model(model_id)
        except Exception:
            pass


if __name__ == "__main__":
    main()
