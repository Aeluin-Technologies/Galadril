"""Face recognition with automatic model download."""

import asyncio
import cv2
from pathlib import Path

from galadril_inference import InferenceEngine, PredictionRequest
from galadril_inference.storage.local import LocalLoader

EXAMPLES_DIR = Path(__file__).parent.resolve()
ARTIFACTS_DIR = EXAMPLES_DIR / "artifacts"
MODEL_DIR = ARTIFACTS_DIR / "face_recognition"
IMAGE_PATH = EXAMPLES_DIR / "images" / "security_council.jpg"


async def main() -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    engine = InferenceEngine(loader=LocalLoader(ARTIFACTS_DIR))
    await engine.load_model("face_recognition")

    image = cv2.imread(str(IMAGE_PATH))
    if image is None:
        print(
            f"Error: could not read image at {IMAGE_PATH}. Check if the file exists."
        )
        return

    result = engine.predict(
        PredictionRequest(
            model_name="face_recognition",
            features={"action": "embed", "image": image},
        )
    )

    faces = result.prediction["faces"]
    print(
        f"\nFound {len(faces)} face(s)  (latency: {result.latency_ms:.1f}ms)\n"
    )

    for i, face in enumerate(faces):
        emb = face.get("embedding")
        print(
            f"  Face {i}: confidence={face['confidence']:.3f}  "
            f"embedding_dim={len(emb) if emb else 0}"
        )


if __name__ == "__main__":
    asyncio.run(main())
