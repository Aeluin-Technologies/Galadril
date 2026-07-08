"""Promptable segmentation and detection comparison."""

from pathlib import Path

import cv2
import numpy as np
from galadril_inference import InferenceEngine, PredictionRequest
from galadril_inference.loading.loader import ArtifactLoader


class HuggingFaceMockLoader(ArtifactLoader):
    def resolve(self, name: str, version: str) -> str:
        return f"{name}_models"

    def exists(self, name: str, version: str) -> bool:
        return name in ("grounded_sam", "owlv2")


EXAMPLES_DIR = Path(__file__).parent.resolve()
IMAGE_PATH = EXAMPLES_DIR / "images" / "military_camp.png"
OUTPUT_PATH_DINO = EXAMPLES_DIR / "images" / "military_camp_detected-dino.png"
OUTPUT_PATH_OWL = EXAMPLES_DIR / "images" / "military_camp_detected-owl.png"

COLORS = {
    "soldier": (0, 255, 0),
    "tent": (0, 0, 255),
    "satellite dish": (255, 0, 0),
    "default": (255, 255, 0),
}


def draw_and_save_predictions(
    image_bgr, result, output_path, model_name
) -> None:
    """Helper function to draw bounding boxes/masks and save the image."""
    concepts = result.prediction["concepts"]
    canvas = image_bgr.copy()

    print(
        f"\n[{model_name}] Total objects found: {result.prediction['total_objects']} (Latency: {result.latency_ms:.1f}ms):"
    )

    for name, data in concepts.items():
        color = COLORS.get(name.lower(), COLORS["default"])
        print(f"  {name}: {data['count']} found")

        for _i, inst in enumerate(data["instances"]):
            box = inst["box"]
            x1, y1, x2, y2 = map(int, box)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)

            if inst.get("mask") is not None:
                mask_np = np.array(inst["mask"], dtype=np.uint8)
                contours, _ = cv2.findContours(
                    mask_np, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                cv2.drawContours(canvas, contours, -1, color, 2)

            label = f"{name} {inst['score']:.2f}"
            (w, h), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(canvas, (x1, y1 - h - 5), (x1 + w, y1), color, -1)
            cv2.putText(
                canvas,
                label,
                (x1, y1 - 5),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (255, 255, 255),
                1,
            )

    cv2.imwrite(str(output_path), canvas)
    print(f"[{model_name}] Image saved to: {output_path}")


def main() -> None:
    """Run Inference for both Grounded SAM and OwlV2."""
    IMAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not IMAGE_PATH.exists():
        print(f"Please place an image file at {IMAGE_PATH} before running.")
        return

    loader = HuggingFaceMockLoader()
    engine = InferenceEngine(loader=loader)

    image_bgr = cv2.imread(str(IMAGE_PATH))
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    concepts_to_find = "soldier. tent. satellite dish"

    print(f"\nDetecting '{concepts_to_find}'...")

    engine.load_model("grounded_sam")
    request_dino = PredictionRequest(
        model_name="grounded_sam",
        features={
            "image": image_rgb,
            "text": concepts_to_find,
            "threshold": 0.3,
            "use_tiling": True,
            "tile_size": 640,
            "tile_overlap": 0.25,
            "nms_threshold": 0.25,
            "return_masks": False,
        },
    )
    result_dino = engine.predict(request_dino)
    draw_and_save_predictions(
        image_bgr, result_dino, OUTPUT_PATH_DINO, "Grounded SAM"
    )
    engine.unload_model("grounded_sam")

    engine.load_model("owlv2")
    request_owl = PredictionRequest(
        model_name="owlv2",
        features={
            "image": image_rgb,
            "text": concepts_to_find,
            "threshold": 0.2,
        },
    )
    result_owl = engine.predict(request_owl)
    draw_and_save_predictions(image_bgr, result_owl, OUTPUT_PATH_OWL, "OwlV2")
    engine.unload_model("owlv2")


if __name__ == "__main__":
    main()
