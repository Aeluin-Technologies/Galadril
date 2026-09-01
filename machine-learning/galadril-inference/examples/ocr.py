"""Read research paper documents using GLM-OCR."""

import asyncio
from pathlib import Path

import cv2
from galadril_inference import InferenceEngine, PredictionRequest
from galadril_inference.loading.loader import ArtifactLoader


class HuggingFaceMockLoader(ArtifactLoader):
    """Resolve the GLM-OCR model directly from Hugging Face."""

    async def resolve(self, name: str, version: str) -> str:
        """Return the Hugging Face repository identifier."""
        return "zai-org/GLM-OCR"

    async def exists(self, name: str, version: str) -> bool:
        """Report whether the requested model is available."""
        return name == "glm_ocr"

    async def upload(
        self,
        model_name: str,
        version: str,
        local_path: str,
    ) -> None:
        """Uploading is not supported by this example loader."""
        return None


EXAMPLES_DIR = Path(__file__).parent.resolve()
IMAGE_PATH = EXAMPLES_DIR / "images" / "paper.png"


async def main() -> None:
    """Run GLM-OCR inference on a single research-paper page."""
    IMAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not IMAGE_PATH.exists():
        print(f"{IMAGE_PATH} not found.")
        return

    image_bgr = cv2.imread(str(IMAGE_PATH), cv2.IMREAD_COLOR)
    if image_bgr is None:
        print(f"Could not read image at {IMAGE_PATH}.")
        return

    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

    engine = InferenceEngine(loader=HuggingFaceMockLoader())

    try:
        await engine.load_model("glm_ocr")

        result = engine.predict(
            PredictionRequest(
                model_name="glm_ocr",
                features={
                    "image": image_rgb,
                    "task": "text",
                    "max_new_tokens": 4096,
                },
            )
        )

        print(result.prediction["text"])
    finally:
        engine.unload_model("glm_ocr")


if __name__ == "__main__":
    asyncio.run(main())
