"""Example: TimesFM weather prevision on Paris."""

import json
import urllib.request
from pathlib import Path

from galadril_inference import InferenceEngine, PredictionRequest
from galadril_inference.storage.local import LocalLoader

EXAMPLES_DIR = Path(__file__).parent.resolve()
ARTIFACTS_DIR = EXAMPLES_DIR / "artifacts"
MODEL_ARTIFACT_PATH = ARTIFACTS_DIR / "timesfm_forecast"


def ensure_artifact_dir() -> None:
    """Create directory structure and metadata for LocalLoader to resolve v1.0.0."""
    MODEL_ARTIFACT_PATH.mkdir(parents=True, exist_ok=True)
    version_dir = MODEL_ARTIFACT_PATH / "1.0.0"
    version_dir.mkdir(exist_ok=True)
    (version_dir / ".ready").touch()
    metadata_file = MODEL_ARTIFACT_PATH / "metadata.json"
    if not metadata_file.exists():
        meta_content = {
            "name": "timesfm_forecast",
            "version": "1.0.0",
            "format": "onnx",
        }
        metadata_file.write_text(json.dumps(meta_content, indent=2))


def fetch_weather_data(
    lat: float, lon: float, past_days: int = 8
) -> tuple[list[float], list[str]]:
    """Fetch history including today's actual data for comparison."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&"
        f"past_days={past_days}&forecast_days=0&"
        f"hourly=temperature_2m"
    )
    print(
        f"Fetching weather data (History + Today) for Lat: {lat}, Lon: {lon}..."
    )

    req = urllib.request.Request(
        url, headers={"User-Agent": "Galadril-Inference/1.0"}
    )
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode("utf-8"))

    temps = data.get("hourly", {}).get("temperature_2m", [])
    times = data.get("hourly", {}).get("time", [])

    return temps, times


def main() -> None:
    ensure_artifact_dir()

    # Paris coordinates.
    LATITUDE, LONGITUDE = 48.8566, 2.3522

    all_temps, all_times = fetch_weather_data(LATITUDE, LONGITUDE, past_days=8)

    actual_today = all_temps[-24:]
    history_until_yesterday = all_temps[:-24]
    today_timestamps = all_times[-24:]

    engine = InferenceEngine(loader=LocalLoader(ARTIFACTS_DIR))
    engine.load_model("timesfm_forecast")

    print(
        f"Forecasting today's temperatures based on previous {len(history_until_yesterday)} hours..."
    )
    result = engine.predict(
        PredictionRequest(
            model_name="timesfm_forecast",
            features={
                "history": [
                    t for t in history_until_yesterday if t is not None
                ],
                "horizon": 24,
            },
        )
    )

    point_forecasts = result.prediction["point_forecast"]
    quantiles = result.prediction["quantiles"]

    print(f"\nComparison: Today's Forecast vs Actual (Paris)")
    print(f"{'Time':<20} | {'Actual':<10} | {'Forecast (± CI)':<20}")
    print("-" * 55)

    for i in range(0, 24, 3):
        time_str = today_timestamps[i].replace("T", " ")
        actual = (
            f"{actual_today[i]:.1f}°C" if actual_today[i] is not None else "N/A"
        )

        point = point_forecasts[i]

        p10 = quantiles[i][1]
        p90 = quantiles[i][9]

        margin = (p90 - p10) / 2
        forecast_str = f"{point:.1f} ± {margin:.1f} °C"

        print(f"{time_str:<20} | {actual:<10} | {forecast_str:<20}")

    print(f"Latency: {result.latency_ms:.1f}ms")


if __name__ == "__main__":
    main()
