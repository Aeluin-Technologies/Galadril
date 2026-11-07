"""Project original texts in a credential-free process before chat inference."""

import base64
import contextlib
import os
import resource
import sys
from pathlib import Path

from galadril_scribe.private_names import pseudonymize
from galadril_scribe.redaction_contracts import (
    MAX_DOCUMENT_BYTES,
    MAX_WIRE_BYTES,
    PRIVATE_BATCH,
    PRIVATE_RESULT,
)

_MAX_CHARS = 192 * 1024
_MAX_SOURCE_CHARS = 65536


def _extract(value: str) -> str:
    import pypdfium2 as pdfium

    data = base64.b64decode(value, validate=True)
    if not data.startswith(b"%PDF-") or len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("Private PDF exceeds limits")
    pages: list[str] = []
    total = 0
    with pdfium.PdfDocument(data) as document:
        if not 1 <= len(document) <= 128:
            raise ValueError("Private PDF page limit")
        for index in range(len(document)):
            page = document[index]
            with (
                contextlib.closing(page),
                contextlib.closing(page.get_textpage()) as text,
            ):
                if text.count_chars() > _MAX_SOURCE_CHARS - total:
                    raise ValueError("Private PDF text limit")
                content = text.get_text_range().replace("\f", " ")
                total += len(content) + 1
                pages.append(content)
    if not any(page.strip() for page in pages):
        raise ValueError("Private PDF requires a readable text layer")
    return "\f".join(pages)


def _limit() -> None:
    if sys.platform != "linux":
        raise ValueError("Private worker requires Linux resource limits")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    for limit, maximum in (
        (resource.RLIMIT_CPU, 100),
        (resource.RLIMIT_AS, 5 * 1024**3),
    ):
        _, hard = resource.getrlimit(limit)
        ceiling = (
            maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
        )
        resource.setrlimit(limit, (ceiling, ceiling))


def main() -> None:
    try:
        _limit()
        directory = Path(sys.argv[1]).resolve(strict=True)
        os.chdir(directory)
        with contextlib.redirect_stdout(sys.stderr):
            wheel = Path(__file__).with_name("private_gliner.whl")
            if wheel.is_file():
                sys.path.insert(0, str(wheel))
            import torch
            from gliner import GLiNER

            torch.set_num_threads(1)
            torch.set_num_interop_threads(1)
            model = GLiNER.from_pretrained(
                str(directory),
                local_files_only=True,
                map_location="cpu",
                strict=True,
                low_cpu_mem_usage=True,
            )
            model.eval()

            # Torch needs temporary files during import, before private input is read.
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
            payload = sys.stdin.buffer.read(MAX_WIRE_BYTES + 1)
            if len(payload) > MAX_WIRE_BYTES:
                raise ValueError("Private input exceeds limit")
            batch = PRIVATE_BATCH.validate_json(payload)
            del payload
            sources = tuple(
                value if kind == "text" else _extract(value)
                for kind, value in batch
            )
            del batch
            if sum(map(len, sources)) > _MAX_CHARS or any(
                len(s) > _MAX_SOURCE_CHARS for s in sources
            ):
                raise ValueError("Private input exceeds text limit")

            def detect(text: str) -> list[tuple[int, int]]:
                return [
                    (item["start"], item["end"])
                    for item in model.predict_entities(
                        text, ["person"], threshold=0.7
                    )
                ]

            with torch.inference_mode():
                result = tuple(
                    "\f".join(
                        pseudonymize(page, detect) for page in text.split("\f")
                    )
                    for text in sources
                )
        sys.stdout.buffer.write(PRIVATE_RESULT.dump_json(result))
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
