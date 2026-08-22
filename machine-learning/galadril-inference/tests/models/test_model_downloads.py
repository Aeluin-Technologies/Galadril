from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

if "structlog" not in sys.modules:
    fake_structlog = types.ModuleType("structlog")

    class _NullLogger:
        def debug(self, *args: object, **kwargs: object) -> None:
            return None

        def info(self, *args: object, **kwargs: object) -> None:
            return None

        def warning(self, *args: object, **kwargs: object) -> None:
            return None

        def exception(self, *args: object, **kwargs: object) -> None:
            return None

    fake_structlog.get_logger = lambda *args, **kwargs: _NullLogger()
    sys.modules["structlog"] = fake_structlog


def _install_fake_huggingface(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a small Hugging Face stub that materializes local files."""
    fake_hf = types.ModuleType("huggingface_hub")

    def snapshot_download(
        repo_id: str,
        local_dir: str,
        allow_patterns: list[str] | None = None,
    ) -> str:
        root = Path(local_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "snapshot.marker").write_text(repo_id, encoding="utf-8")
        return str(root)

    def hf_hub_download(
        repo_id: str,
        filename: str,
        local_dir: str,
    ) -> str:
        root = Path(local_dir)
        dest = root / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(repo_id, encoding="utf-8")
        return str(dest)

    fake_hf.snapshot_download = snapshot_download
    fake_hf.hf_hub_download = hf_hub_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)


def _install_fake_faster_whisper(
    monkeypatch: pytest.MonkeyPatch, written: dict[str, object]
) -> None:
    """Install a minimal faster-whisper stub that writes a marker file."""
    fake_fw = types.ModuleType("faster_whisper")

    class FakeWhisperModel:
        def __init__(
            self,
            *,
            model_size_or_path: str,
            device: str,
            compute_type: str,
            download_root: str,
        ) -> None:
            written["args"] = {
                "model_size_or_path": model_size_or_path,
                "device": device,
                "compute_type": compute_type,
                "download_root": download_root,
            }
            root = Path(download_root)
            root.mkdir(parents=True, exist_ok=True)
            (root / "model.marker").write_text("whisper", encoding="utf-8")

    fake_fw.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)


def test_siglip_download_creates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SigLIP download should materialize a non-empty artifact tree."""
    _install_fake_huggingface(monkeypatch)
    from galadril_inference.models.embedding.siglip import SigLIPModel

    model = SigLIPModel()
    model.download(str(tmp_path))

    assert (tmp_path / "snapshot.marker").read_text() == (
        "onnx-community/siglip2-base-patch16-384-ONNX"
    )


def test_qwen3_download_creates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Qwen3 download should materialize the selected GGUF file."""
    _install_fake_huggingface(monkeypatch)
    from galadril_inference.models.embedding.qwen3 import Qwen3EmbeddingModel

    model = Qwen3EmbeddingModel()
    model.download(str(tmp_path))

    assert (tmp_path / "Qwen3-Embedding-0.6B-Q6_K.gguf").exists()


def test_bgem3_download_creates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BGE-M3 download should persist tokenizer and ONNX assets."""
    _install_fake_huggingface(monkeypatch)
    from galadril_inference.models.embedding.bgem3 import BgeM3Model

    model = BgeM3Model()
    model.download(str(tmp_path))

    assert (tmp_path / "tokenizer" / "snapshot.marker").read_text() == (
        "BAAI/bge-m3"
    )
    assert (tmp_path / "onnx" / "model.onnx").exists()


def test_got_ocr_download_creates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GOT-OCR download should persist the snapshot locally."""
    _install_fake_huggingface(monkeypatch)
    from galadril_inference.models.text.got_ocr import GotOcrModel

    model = GotOcrModel()
    model.download(str(tmp_path))

    assert (tmp_path / "snapshot.marker").read_text() == (
        "stepfun-ai/GOT-OCR-2.0-hf"
    )


def test_timesfm_download_creates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TimesFM download should persist ONNX assets locally."""
    _install_fake_huggingface(monkeypatch)
    from galadril_inference.models.temporal.time_series import TimesFMModel

    model = TimesFMModel()
    model.download(str(tmp_path))

    assert (tmp_path / "snapshot.marker").read_text() == (
        "pdufour/timesfm-2.5-200m-transformers-onnx"
    )


def test_owl_download_creates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OwlV2 download should persist the checkpoint locally."""
    _install_fake_huggingface(monkeypatch)
    from galadril_inference.models.image.owl import OwlV2Model

    model = OwlV2Model()
    model.download(str(tmp_path))

    assert (tmp_path / "snapshot.marker").read_text() == (
        "onnx-community/owlv2-base-patch16-ensemble-ONNX"
    )


def test_grounded_sam_download_creates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grounded SAM download should materialize both upstream bundles."""
    _install_fake_huggingface(monkeypatch)
    from galadril_inference.models.image.grounded_sam import GroundedSamModel

    model = GroundedSamModel()
    model.download(str(tmp_path))

    assert (tmp_path / "grounding-dino" / "snapshot.marker").read_text() == (
        "onnx-community/grounding-dino-tiny-ONNX"
    )
    assert (tmp_path / "sam-vit-base" / "snapshot.marker").read_text() == (
        "onnx-community/sam-vit-base-ONNX"
    )


def test_whisper_download_creates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whisper download should persist both transcription and diarization assets."""
    _install_fake_huggingface(monkeypatch)
    written: dict[str, object] = {}
    _install_fake_faster_whisper(monkeypatch, written)
    from galadril_inference.models.audio.whisper import WhisperModel

    model = WhisperModel()
    model.download(str(tmp_path))

    assert (tmp_path / "whisper" / "model.marker").read_text() == "whisper"
    assert (tmp_path / "diarization" / "onnx" / "model.onnx").exists()


def test_geoclip_download_creates_manifest(
    tmp_path: Path,
) -> None:
    """GeoCLIP download should create a small manifest for bootstrap flows."""
    from galadril_inference.models.osint.geoclip import GeoCLIPModel

    model = GeoCLIPModel()
    model.download(str(tmp_path))

    manifest = tmp_path / "artifact_manifest.json"
    assert manifest.exists()
    assert '"model_name": "geoclip"' in manifest.read_text()


def test_geoclip_auto_device_prefers_available_accelerator() -> None:
    """GeoCLIP should select CUDA and otherwise fall back without failing load."""
    from galadril_inference.models.osint.geoclip import GeoCLIPModel

    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: True),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False)
        ),
    )

    assert GeoCLIPModel._resolve_device(fake_torch, "auto") == "cuda"
    assert GeoCLIPModel._resolve_device(fake_torch, "mps") == "cpu"
