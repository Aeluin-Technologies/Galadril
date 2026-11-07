"""Install the pinned CPU detector before starting Scribe."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from urllib.request import urlopen

_GLINER = "https://huggingface.co/urchade/gliner_multi-v2.1/resolve/443d26d654e0324125a96bebd8e796c14ff2efe6/"
_TOKENIZER = "https://huggingface.co/microsoft/mdeberta-v3-base/resolve/a0484667b22365f84929a935b5e50a51f71f159d/"
_FILES = (
    (
        _GLINER,
        "gliner_config.json",
        "e25f61d91620df84aae8076811ee592e926e94d341b82e7bc1be359718f83017",
        477,
    ),
    (
        _GLINER,
        "model.safetensors",
        "2100142f31627531497850659dcb3821c99d5e71c08a8e01a98e4b11ef32a199",
        1155830112,
    ),
    (
        _TOKENIZER,
        "config.json",
        "bcffcd343dc5efa5ef2d5a58d2b405eed108f01cc45b48d0a907b333ec41801f",
        579,
    ),
    (
        _TOKENIZER,
        "tokenizer_config.json",
        "3f3978e0c036f2c2588cac34a6047cbb0af0b0dc1814254e291028529805496d",
        52,
    ),
    (
        _TOKENIZER,
        "spm.model",
        "13c8d666d62a7bc4ac8f040aab68e942c861f93303156cc28f5c7e885d86d6e3",
        4305025,
    ),
)


def install(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination) as temporary:
        staging = Path(temporary)
        for origin, name, checksum, expected_size in _FILES:
            digest = hashlib.sha256()
            size = 0
            with (
                urlopen(origin + name, timeout=60) as response,
                (staging / name).open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > expected_size:
                        raise ValueError(f"Unexpected size for {name}")
                    digest.update(chunk)
                    output.write(chunk)
            if size != expected_size or digest.hexdigest() != checksum:
                raise ValueError(f"Checksum mismatch for {name}")
        config = staging / "gliner_config.json"
        values = json.loads(config.read_text(encoding="utf-8"))
        values["model_name"] = "."
        config.write_text(json.dumps(values) + "\n", encoding="utf-8")
        for _, name, _, _ in _FILES:
            (staging / name).replace(destination / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    install(parser.parse_args().destination)
