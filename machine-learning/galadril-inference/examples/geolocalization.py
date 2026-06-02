"""Approximative image location using GeoCLIP."""

from pathlib import Path
import math
import numpy as np

from galadril_inference.common.types import PredictionRequest
from galadril_inference.core.engine import InferenceEngine
from galadril_inference.loading.loader import ArtifactLoader

EXAMPLES_DIR = Path(__file__).parent.resolve()
IMAGE_PATH = EXAMPLES_DIR / "images" / "geo.jpg"

NANTES_LAT = 47.2023
NANTES_LON = -1.5369
EARTH_RADIUS_KM = 6371.0


class LocalArtifactLoader(ArtifactLoader):
    def resolve(self, name: str, version: str) -> str:
        return str(EXAMPLES_DIR / "artifacts")

    def exists(self, name: str, version: str) -> bool:
        return name == "geoclip"


def haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    p_lat1, p_lon1 = math.radians(lat1), math.radians(lon1)
    p_lat2, p_lon2 = math.radians(lat2), math.radians(lon2)

    dlat = p_lat2 - p_lat1
    dlon = p_lon2 - p_lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(p_lat1) * math.cos(p_lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def main():
    loader = LocalArtifactLoader()
    engine = InferenceEngine(loader=loader)
    engine.load_model("geoclip", device="cpu")

    if not IMAGE_PATH.exists():
        print(f"Error: Target image not found at {IMAGE_PATH}")
        return

    features = {
        "task": "image_to_gps",
        "image_path": str(IMAGE_PATH),
        "top_k": 3,
    }

    req = PredictionRequest(model_name="geoclip", features=features)

    result = engine.predict(req)
    predictions = result.prediction["predictions"]

    print("--- Top GPS Predictions ---")
    for i, pred in enumerate(predictions):
        print(
            f"[{i + 1}] Coordinates: ({pred['latitude']:.4f}, {pred['longitude']:.4f}) | Probability: {pred['probability']:.4f}"
        )

    top_pred = predictions[0]
    pred_lat = top_pred["latitude"]
    pred_lon = top_pred["longitude"]

    distance = haversine_distance(pred_lat, pred_lon, NANTES_LAT, NANTES_LON)

    print("\n--- Localization Verification ---")
    print(
        f"Target: Boulevard Paul Langevin, Nantes [{NANTES_LAT}, {NANTES_LON}]"
    )
    print(f"Prediction: [{pred_lat:.4f}, {pred_lon:.4f}]")
    print(f"Distance Error: {distance:.2f} km")

    engine.unload_model("geoclip")


if __name__ == "__main__":
    main()
