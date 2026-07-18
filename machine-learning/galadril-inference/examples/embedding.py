"""Example script demonstrating cross-lingual multimodal vector search with Qwen3-Embedding."""

import asyncio
from pathlib import Path
import numpy as np

from galadril_inference.common.types import PredictionRequest
from galadril_inference.core.engine import InferenceEngine
from galadril_inference.loading.loader import ArtifactLoader

EXAMPLES_DIR = Path(__file__).parent.resolve()
IMAGE_PATH = EXAMPLES_DIR / "images" / "gazelle.png"

MULTILINGUAL_CANDIDATES = {
    "Français (True)": "Un hélicoptère militaire Sud-Aviation SA342 Gazelle en plein vol.",
    "English (True)": "A Sud-Aviation SA342 Gazelle military helicopter in mid-flight.",
    "Korean (True)": "비행 중인 수드 아비아시옹 SA342 가젤 군용 헬리콥터.",
    "Japanese (True)": "飛行中のシュド・アビアシオン SA342 ガゼル軍用ヘリコプター。",
    "Français (Fake)": "Un gros camion de pompier rouge qui roule en ville.",
    "English (Fake)": "A large red firetruck driving through the city.",
}


class LocalArtifactLoader(ArtifactLoader):
    async def resolve(self, name: str, version: str) -> str:
        return str(EXAMPLES_DIR / "artifacts")

    async def exists(self, name: str, version: str) -> bool:
        return name == "qwen_embedding"

    async def upload(
        self, model_name: str, version: str, local_path: str
    ) -> None:
        pass


def get_embedding(
    engine: InferenceEngine, text_content: str, task: str | None = None
) -> np.ndarray:
    feat = {
        "text": text_content,
        "dimensions": 1024,
    }
    if task:
        feat["task"] = task

    req = PredictionRequest(model_name="qwen_embedding", features=feat)
    return np.array(engine.predict(req).prediction["embedding"])


async def main():
    loader = LocalArtifactLoader()
    engine = InferenceEngine(loader=loader)

    await engine.load_model(
        "qwen_embedding", model_tier="0.6b", compute_type="q6_k"
    )

    image_payload = f"Picture: {IMAGE_PATH}"
    img_vec = get_embedding(engine, text_content=image_payload)
    print(f"Image Vector Shape: {img_vec.shape}")
    print("-" * 60)

    results = []
    task_description = (
        "Given an image retrieval query, find the matching textual description."
    )

    for label, text in MULTILINGUAL_CANDIDATES.items():
        txt_vec = get_embedding(
            engine, text_content=text, task=task_description
        )

        score = float(np.dot(img_vec, txt_vec))
        results.append((score, label, text))

    print("Latent space results:")
    for score, label, text in sorted(results, reverse=True):
        print(f"  Score: {score:.4f} | [{label}] -> '{text[:50]}...'")

    engine.unload_model("qwen_embedding")


if __name__ == "__main__":
    asyncio.run(main())
