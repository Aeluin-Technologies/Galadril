"""Speech recognition with optional Diarization and Speaker Embeddings."""

import asyncio
from pathlib import Path

import soundfile as sf

from galadril_inference import InferenceEngine, PredictionRequest
from galadril_inference.loading.loader import ArtifactLoader


class HuggingFaceMockLoader(ArtifactLoader):
    async def resolve(self, name: str, version: str) -> str:
        return str(Path(__file__).parent.resolve() / "artifacts" / "whisper")

    async def exists(self, name: str, version: str) -> bool:
        return name == "whisper"

    async def upload(
        self, model_name: str, version: str, local_path: str
    ) -> None:
        pass


EXAMPLES_DIR = Path(__file__).parent.resolve()
AUDIO_PATH = EXAMPLES_DIR / "audio" / "galadriel.mp3"


async def main() -> None:
    AUDIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not AUDIO_PATH.exists():
        print(f"No file found at {AUDIO_PATH}.")
        return

    loader = HuggingFaceMockLoader()
    engine = InferenceEngine(loader=loader)
    await engine.load_model("whisper")
    waveform, sr = sf.read(str(AUDIO_PATH))

    request = PredictionRequest(
        model_name="whisper",
        features={
            "audio": {"waveform": waveform, "sample_rate": sr},
            "task": "transcribe",
            "enable_diarization": True,
        },
    )

    result = engine.predict(request)
    prediction = result.prediction

    current_speaker = None
    speaker_embeddings = {}

    for chunk in prediction["chunks"]:
        ts = chunk["timestamp"]
        end_time = f"{ts[1]:.2f}" if ts[1] else "end"
        text = chunk["text"].strip()

        if "speaker" in chunk:
            speaker = chunk.get("speaker", "UNKNOWN")
            if (
                "speaker_embedding" in chunk
                and chunk["speaker_embedding"] is not None
            ):
                if speaker not in speaker_embeddings:
                    speaker_embeddings[speaker] = chunk["speaker_embedding"]

            if speaker != current_speaker:
                print(f"\n[{speaker}]:")
                current_speaker = speaker
            print(f"  ({ts[0]:.2f}s - {end_time}s) {text}")
        else:
            print(f"[{ts[0]:.2f}s - {end_time}s] {text}")

    if speaker_embeddings:
        print("\n--- Speakers ---")
        for spk, emb in speaker_embeddings.items():
            print(f"Speaker {spk} : embedding = {len(emb)} dimensions")

    print(f"\nLatency: {result.latency_ms:.2f} ms")

    engine.unload_model("whisper")


if __name__ == "__main__":
    asyncio.run(main())
