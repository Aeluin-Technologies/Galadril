"""Read research paper documents using GOT-OCR."""

import asyncio
import cv2
from pathlib import Path

from galadril_inference import InferenceEngine, PredictionRequest
from galadril_inference.loading.loader import ArtifactLoader


class HuggingFaceMockLoader(ArtifactLoader):
    async def resolve(self, name: str, version: str) -> str:
        return "stepfun-ai/GOT-OCR-2.0-hf"

    async def exists(self, name: str, version: str) -> bool:
        return name == "got_ocr"

    async def upload(
        self, model_name: str, version: str, local_path: str
    ) -> None:
        pass


EXAMPLES_DIR = Path(__file__).parent.resolve()
IMAGE_PATH = EXAMPLES_DIR / "images" / "paper.png"


async def main() -> None:
    """Run GOT-OCR inference on a single CIA document page."""
    IMAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not IMAGE_PATH.exists():
        print(f"{IMAGE_PATH} not found.")
        return

    engine = InferenceEngine(loader=HuggingFaceMockLoader())
    await engine.load_model("got_ocr")

    image_bgr = cv2.imread(str(IMAGE_PATH))
    if image_bgr is None:
        print(f"Could not read image at {IMAGE_PATH}.")
        return

    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    result_single = engine.predict(
        PredictionRequest(
            model_name="got_ocr",
            features={
                "image": image_rgb,
                "format": True,
            },
        )
    )
    print(result_single.prediction["text"])

    engine.unload_model("got_ocr")


if __name__ == "__main__":
    asyncio.run(main())
