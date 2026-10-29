"""Document ranking preserves evidence IDs and only traverses the supplied scope."""

import base64
import hashlib
import json
import sys

import pytest
from eru.documents import ExtractionPolicy, Projection
from galadril_scribe.retrieval import (
    Fact,
    Passage,
    fuse_ranks,
    structural_chunks,
)
from galadril_scribe.retrieval_contracts import (
    EmbeddedTexts,
    EmbedTexts,
    ParseDocument,
    RankPassages,
    RecognizedFacts,
    RecognizeFacts,
)
from galadril_scribe.retrieval_runtime import DocumentRuntime
from pydantic_ai.models.test import TestModel
from pydantic_ai.profiles import ModelProfile


@pytest.mark.anyio
async def test_background_document_work_cannot_block_interactive_admission() -> (
    None
):
    import asyncio

    import httpx
    from fastapi import FastAPI
    from galadril_scribe.retrieval_api import install_documents
    from galadril_scribe.retrieval_contracts import ParsedDocument

    entered = asyncio.Semaphore(0)
    release = asyncio.Event()

    class Runtime(DocumentRuntime):
        async def parse(self, request: ParseDocument) -> ParsedDocument:
            entered.release()
            await release.wait()
            return ParsedDocument(units=(), warnings=())

        async def embed(self, request: EmbedTexts) -> EmbeddedTexts:
            return EmbeddedTexts(profile_id="a" * 64, vectors=((1.0,),))

    app = FastAPI()
    install_documents(app, Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://scribe"
    ) as client:
        body = {
            "mime": "text/plain",
            "content": base64.b64encode(b"evidence").decode(),
            "checksum": hashlib.sha256(b"evidence").hexdigest(),
        }
        pending = [
            asyncio.create_task(client.post("/documents/parse", json=body))
            for _ in range(2)
        ]
        try:
            for _ in pending:
                await asyncio.wait_for(entered.acquire(), timeout=1)
            assert (
                await client.post("/documents/parse", json=body)
            ).status_code == 429
            assert (
                await client.post(
                    "/documents/embed", json={"texts": ["query"], "query": True}
                )
            ).status_code == 200
        finally:
            release.set()
            await asyncio.gather(*pending)


def test_recognition_anchors_distinctive_names_without_matching_substrings() -> (
    None
):
    from galadril_scribe.retrieval_contracts import RecognizeFacts
    from galadril_scribe.retrieval_eru import anchored_indices

    request = RecognizeFacts(
        query="Where does Alice work?",
        facts=(
            {
                "passage_id": "a" * 64,
                "ordinal": 0,
                "fact": {
                    "subject": "Alice",
                    "predicate": "works at",
                    "object": "Laboratory",
                },
                "similarity": 0.8,
            },
            {
                "passage_id": "b" * 64,
                "ordinal": 0,
                "fact": {
                    "subject": "Malice",
                    "predicate": "works at",
                    "object": "Laboratory",
                },
                "similarity": 0.8,
            },
        ),
    )
    assert anchored_indices(request) == (0,)


@pytest.mark.anyio
async def test_local_extraction_precedes_chat_and_rejected_relations_are_visible() -> (
    None
):
    from eru.documents import ExtractionPolicy, Projection
    from eru.documents import Fact as EruFact
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage

    class Knowledge:
        async def extract(
            self, text: str, policy: ExtractionPolicy | None = None
        ) -> Projection:
            return Projection(
                facts=(
                    EruFact(
                        subject="Alice", predicate="works in", object="Lorraine"
                    ),
                ),
                candidates=2,
                rejected=1,
            )

        async def recognize(self, request: RecognizeFacts) -> RecognizedFacts:
            raise AssertionError("Recognition was not requested")

    runtime = DocumentRuntime(knowledge=Knowledge())
    result = await runtime.extract(
        ExtractFacts(
            passages=(
                ExtractPassage(id="a" * 64, text="Alice works in Lorraine"),
            )
        )
    )
    assert result.passages[0].facts[0].subject == "Alice"
    assert "graph_relations_rejected" in result.warnings


@pytest.mark.anyio
async def test_local_extraction_failure_returns_partial_evidence_without_failing_conversation() -> (
    None
):
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage

    class Knowledge:
        async def extract(
            self, text: str, policy: ExtractionPolicy | None = None
        ) -> Projection:
            raise RuntimeError("private-document")

        async def recognize(self, request: RecognizeFacts) -> RecognizedFacts:
            raise RuntimeError("private-document")

    result = await DocumentRuntime(knowledge=Knowledge()).extract(
        ExtractFacts(
            passages=(
                ExtractPassage(id="a" * 64, text="Alice works in Lorraine"),
            )
        )
    )
    assert result.passages[0].facts == ()
    assert result.warnings == ("graph_extraction_unavailable",)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_remote_relations_skip_local_reasoner_and_enforce_native_schema() -> (
    None
):
    from eru.documents import Entity
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    calls: list[str] = []

    class Knowledge:
        async def entities(
            self, text: str, policy: ExtractionPolicy | None = None
        ) -> tuple[Entity, ...]:
            calls.append("entities")
            return (Entity(id="ent_0", text="Alice", type="PERSON"),)

        async def extract(
            self, text: str, policy: ExtractionPolicy | None = None
        ) -> Projection:
            raise AssertionError(
                "Remote mode must not run local relations first"
            )

        async def recognize(self, request: RecognizeFacts) -> RecognizedFacts:
            raise AssertionError("Recognition was not requested")

    def remote(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append("remote")
        assert info.model_request_parameters.output_mode == "native"
        assert info.model_request_parameters.output_object is not None
        assert info.model_request_parameters.output_object.strict is True
        assert "Alice" in str(messages)
        return ModelResponse(
            parts=[
                TextPart(
                    content='{"facts":[{"subject":"Alice","subject_type":"PERSON","predicate":"visits","object":"Paris","object_type":"LOCATION","evidence":"Alice visits Paris"}]}'
                )
            ]
        )

    runtime = DocumentRuntime(
        knowledge=Knowledge(), relation_model=FunctionModel(remote)
    )
    result = await runtime.extract(
        ExtractFacts(
            passages=(ExtractPassage(id="a" * 64, text="Alice visits Paris"),)
        )
    )
    assert calls == ["entities", "remote"]
    assert result.passages[0].facts[0].object == "Paris"


def test_remote_relation_extraction_is_enabled_by_default_and_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_scribe.contracts import Settings

    settings = Settings(gateway_tools_url="http://gateway/internal/chat/tools")
    assert settings.document_remote_relations
    assert settings.document_relation_model == "gpt-5.6-terra"
    monkeypatch.setenv("SCRIBE_DOCUMENT_REMOTE_RELATIONS", "false")
    assert not Settings(
        gateway_tools_url="http://gateway/internal/chat/tools"
    ).document_remote_relations


@pytest.mark.anyio
async def test_cancelled_native_extraction_retains_admission_until_worker_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    from galadril_scribe.retrieval_eru import LocalEru

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocking(
        self: LocalEru, text: str, policy: ExtractionPolicy | None
    ) -> Projection:
        calls.append(text)
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=5):
            raise RuntimeError("Native test worker was not released")
        return Projection(facts=(), candidates=0, rejected=0)

    monkeypatch.setattr(LocalEru, "_extract", blocking)
    knowledge = LocalEru("/unused")
    first = asyncio.create_task(knowledge.extract("first"))
    second: asyncio.Task[Projection] | None = None
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        second = asyncio.create_task(knowledge.extract("second"))
        await asyncio.sleep(0.02)
        assert calls == ["first"]
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second is not None:
            await second


@pytest.mark.anyio
async def test_remote_relations_still_reject_invented_evidence() -> None:
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage

    runtime = DocumentRuntime(
        relation_model=TestModel(
            custom_output_text=json.dumps(
                {
                    "facts": [
                        {
                            "subject": "Invented",
                            "subject_type": "PERSON",
                            "object_type": "LOCATION",
                            "predicate": "owns",
                            "object": "Lorraine",
                            "evidence": "Alice works in Lorraine",
                        }
                    ]
                }
            ),
            profile=ModelProfile(supports_json_schema_output=True),
        )
    )
    result = await runtime.extract(
        ExtractFacts(
            passages=(
                ExtractPassage(id="a" * 64, text="Alice works in Lorraine"),
            )
        )
    )
    assert not result.passages[0].facts
    assert "graph_relations_rejected" in result.warnings


@pytest.mark.anyio
async def test_document_parsing_preserves_provenance_and_verifies_the_source_checksum() -> (
    None
):
    content = "La charte prévoit une procédure en cas d’irrégularité.".encode()
    runtime = DocumentRuntime()
    parsed = await runtime.parse(
        ParseDocument(
            mime="text/plain",
            content=base64.b64encode(content).decode(),
            checksum=hashlib.sha256(content).hexdigest(),
        )
    )
    assert parsed.units[0].text == content.decode()
    assert parsed.units[0].start == 0
    with pytest.raises(ValueError):
        await runtime.parse(
            ParseDocument(
                mime="text/plain",
                content=base64.b64encode(content).decode(),
                checksum="a" * 64,
            )
        )


@pytest.mark.anyio
async def test_hipporag_recognition_and_local_reranking_keep_source_identifiers() -> (
    None
):
    class Reranker:
        profile_id = "local-reranker"

        async def scores(
            self, query: str, documents: tuple[str, ...]
        ) -> tuple[float, ...]:
            return tuple(0.8 for _ in documents)

    calls: list[str] = []

    class Embedder:
        async def embed(self, request: EmbedTexts) -> EmbeddedTexts:
            if not request.query:
                calls.extend(request.texts)

            return EmbeddedTexts(
                profile_id="a" * 64,
                vectors=tuple(
                    tuple(
                        (byte + 1) / 256
                        for byte in hashlib.sha256(text.encode()).digest()[:4]
                    )
                    for text in request.texts
                ),
            )

    first, second = "a" * 64, "b" * 64
    passages = (
        Passage(
            id=first,
            text="Alice directs the laboratory.",
            dense_score=0.9,
            facts=(
                Fact(subject="Alice", predicate="directs", object="Laboratory"),
            ),
        ),
        Passage(
            id=second,
            text="The laboratory belongs to Lorraine.",
            dense_score=0.1,
            facts=(
                Fact(
                    subject="Laboratory",
                    predicate="belongs to",
                    object="Lorraine",
                ),
            ),
        ),
    )
    runtime = DocumentRuntime(
        embedder=Embedder(),
        reranker=Reranker(),
        recognition_model=TestModel(custom_output_args={"indices": [0]}),
    )
    await runtime.embed(
        EmbedTexts(texts=tuple(passage.text for passage in passages))
    )
    result = await runtime.rank(
        RankPassages(
            query="Where is Alice’s laboratory?",
            passages=passages,
            strategy="hipporag2",
            facts=(
                {
                    "passage_id": first,
                    "ordinal": 0,
                    "fact": passages[0].facts[0],
                    "similarity": 0.9,
                },
            ),
        )
    )
    assert {item.id for item in result.passages} == {first, second}
    assert all(item.graph_score is not None for item in result.passages)
    assert result.reranker_profile == "local-reranker"
    assert result.engine == "hipporag2-official"
    assert all(calls.count(passage.text) == 1 for passage in passages)
    await runtime.close()


@pytest.mark.anyio
async def test_empty_official_recognition_reports_dense_fallback_without_fake_graph_scores() -> (
    None
):
    from galadril_scribe.retrieval_contracts import Recognition

    class Embedder:
        async def embed(self, request: EmbedTexts) -> EmbeddedTexts:
            return EmbeddedTexts(
                profile_id="a" * 64,
                vectors=tuple((1.0, 0.0) for _ in request.texts),
            )

    runtime = DocumentRuntime(embedder=Embedder())
    try:
        result = await runtime.rank(
            RankPassages(
                query="Unrelated question",
                strategy="hipporag2",
                recognized=Recognition(),
                passages=(
                    Passage(
                        id="evidence",
                        text="Alice directs Lab",
                        facts=(
                            Fact(
                                subject="Alice",
                                predicate="directs",
                                object="Lab",
                            ),
                        ),
                    ),
                ),
            )
        )
        assert result.engine == "hipporag2-official"
        assert "graph_dense_fallback" in result.warnings
        assert result.passages and all(
            item.graph_score is None for item in result.passages
        )
    finally:
        await runtime.close()


@pytest.mark.anyio
async def test_reranker_failure_degrades_without_losing_the_conversation() -> (
    None
):
    class Reranker:
        profile_id = "unavailable"

        async def scores(
            self, query: str, documents: tuple[str, ...]
        ) -> tuple[float, ...]:
            raise RuntimeError("private backend diagnostics")

    runtime = DocumentRuntime(reranker=Reranker())
    selected = RankPassages(
        query="Proof",
        passages=(Passage(id="proof", text="Evidence", dense_score=0.9),),
    )
    result = await runtime.rank(selected)
    assert result.passages[0].id == "proof"
    assert "reranker_unavailable" in result.warnings


@pytest.mark.anyio
async def test_final_reranker_does_not_add_incommensurate_pagerank_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_scribe.retrieval_contracts import Recognition

    async def graph(
        self: DocumentRuntime, request: RankPassages, selected: Recognition
    ) -> dict[str, float]:
        return {"relevant": 0.001, "decoy": 0.99}

    class Reranker:
        profile_id = "local"

        async def scores(
            self, query: str, documents: tuple[str, ...]
        ) -> tuple[float, ...]:
            return tuple(
                0.8 if text == "Relevant legal ground" else 0.7
                for text in documents
            )

    monkeypatch.setattr(DocumentRuntime, "_official_rank", graph)
    result = await DocumentRuntime(reranker=Reranker()).rank(
        RankPassages(
            query="Legal grounds?",
            strategy="hipporag2",
            recognized=Recognition(),
            passages=(
                Passage(id="relevant", text="Relevant legal ground"),
                Passage(id="decoy", text="Popular but irrelevant"),
            ),
        )
    )
    assert result.passages[0].id == "relevant"
    assert "private backend" not in result.model_dump_json()


def _text_pdf(text: str = "Evidence about Lorraine") -> bytes:
    stream = f"BT /F1 14 Tf 30 100 Td ({text}) Tj ET".encode()
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    )
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(body)


@pytest.mark.anyio
async def test_pdf_parser_preserves_native_text_and_uses_local_ocr_for_visual_pages() -> (
    None
):
    import io

    from galadril_scribe.retrieval_models import PdfParser
    from PIL import Image

    calls: list[bytes] = []

    class Ocr:
        def read(self, image: bytes) -> str:
            calls.append(image)
            with Image.open(io.BytesIO(image)) as decoded:
                assert decoded.width <= 1600 and decoded.height <= 1600
            return "Scanned evidence about Lorraine"

    parser = PdfParser(ocr=Ocr())
    native = await parser.parse(_text_pdf(), "application/pdf")
    assert native[0].text == "Evidence about Lorraine"
    assert native[0].page == 1
    assert not calls
    scanned = await parser.parse(_text_pdf(""), "application/pdf")
    assert scanned[0].page == 1
    assert "Scanned evidence" in scanned[0].text
    image = io.BytesIO()
    Image.new("RGB", (2000, 1800), "white").save(image, format="PNG")
    for mime, format in (
        ("image/png", "PNG"),
        ("image/jpeg", "JPEG"),
        ("image/webp", "WEBP"),
    ):
        content = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(content, format=format)
        units = await parser.parse(content.getvalue(), mime)
        assert units[0].text == "Scanned evidence about Lorraine"
    await parser.parse(image.getvalue(), "image/png")
    with pytest.raises(ValueError):
        await parser.parse(b"not a PDF", "application/pdf")
    with pytest.raises(ValueError):
        await parser.parse(b"image", "image/png")
    with pytest.raises(ValueError):
        await parser.parse(image.getvalue(), "text/html")


@pytest.mark.anyio
async def test_index_parser_accepts_the_library_upload_size_without_unbounded_text() -> (
    None
):
    from galadril_scribe.retrieval_models import PdfParser

    content = _text_pdf() + b" " * (9 * 1024 * 1024)
    parsed = await DocumentRuntime(parser=PdfParser()).parse(
        ParseDocument(
            mime="application/pdf",
            content=base64.b64encode(content).decode(),
            checksum=hashlib.sha256(content).hexdigest(),
        )
    )
    assert parsed.units[0].text == "Evidence about Lorraine"


@pytest.mark.anyio
async def test_private_document_api_has_bounded_inputs_and_no_storage_authority() -> (
    None
):
    import httpx
    from galadril_scribe.contracts import Settings
    from galadril_scribe.runtime import Runtime, create_app

    async with httpx.AsyncClient() as transport:
        runtime = Runtime(
            Settings(gateway_tools_url="http://gateway/internal/chat/tools"),
            transport,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as client:
            content = b"public evidence"
            response = await client.post(
                "/documents/parse",
                json={
                    "mime": "text/plain",
                    "content": base64.b64encode(content).decode(),
                    "checksum": hashlib.sha256(content).hexdigest(),
                },
            )
            assert response.status_code == 200
            assert response.json()["units"][0]["text"] == content.decode()
            response = await client.post(
                "/documents/parse",
                json={"url": "http://storage/private", "tenant": "other"},
            )
            assert response.status_code == 422


def test_structural_chunks_preserve_exact_locations_and_reject_unbounded_input() -> (
    None
):
    text = (
        "Préambule\n\n"
        + "university evidence " * 50
        + "\n\nArticle 2\nAppeals require review."
    )
    chunks = structural_chunks(text, max_characters=256, overlap=32)
    assert len(chunks) > 2
    assert all(text[chunk.start : chunk.end] == chunk.text for chunk in chunks)
    assert all(len(chunk.text) <= 256 for chunk in chunks)
    assert chunks[-1].end == len(text)
    with pytest.raises(ValueError):
        structural_chunks("a" * 1048577)


def test_rank_fusion_is_deterministic_without_comparing_incompatible_scores() -> (
    None
):
    assert fuse_ranks([["a", "b"], ["b", "c"]])[0][0] == "b"
    assert len(fuse_ranks([["a", "a"], ["b"]])) == 2


@pytest.mark.anyio
async def test_cancelled_ocr_retains_admission_until_native_work_finishes() -> (
    None
):
    import asyncio
    import io
    import threading

    from galadril_scribe.retrieval_models import PdfParser
    from PIL import Image

    started = threading.Event()
    finish = threading.Event()

    class Ocr:
        def read(self, image: bytes) -> str:
            started.set()
            if not finish.wait(timeout=5):
                raise RuntimeError("Native OCR was not released")
            return "Evidence"

    content = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(content, format="PNG")
    worker = asyncio.create_task(
        PdfParser(ocr=Ocr()).parse(content.getvalue(), "image/png")
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        worker.cancel()
        await asyncio.sleep(0.01)
        assert not worker.done()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await worker


def test_local_ocr_image_and_video_processors_have_cpu_dependencies() -> None:
    from PIL import Image
    from transformers.models.glm46v.image_processing_glm46v import (
        Glm46VImageProcessor,
    )
    from transformers.models.glm46v.video_processing_glm46v import (
        Glm46VVideoProcessor,
    )

    processor = Glm46VImageProcessor()
    result = processor(
        images=[Image.new("RGB", (112, 112), "white")], return_tensors="pt"
    )
    assert result["pixel_values"].numel() > 0
    assert Glm46VVideoProcessor().merge_size == 2


def test_local_ocr_loads_the_pinned_cpu_model_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    import io

    from galadril_inference.common.types import PredictionResult
    from galadril_scribe import retrieval_ocr
    from PIL import Image

    loaded: list[str] = []

    class Model:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["device"] == "cpu"
            assert (
                kwargs["revision"] == "2e85a62840ccac27daa451df36c736c4636b8628"
            )

        def download(self, path: str) -> None:
            loaded.append("download")

        def load(self, path: str) -> None:
            loaded.append("load")

        def predict(self, request: object) -> PredictionResult:
            return PredictionResult(
                model_name="glm_ocr",
                model_version="1",
                prediction={"text": "Local OCR evidence"},
            )

    parameters: dict[str, object] = {}

    def model(**kwargs: object) -> Model:
        parameters.update(kwargs)
        return Model(**kwargs)

    monkeypatch.setattr(retrieval_ocr, "GlmOcrModel", model)
    content = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(content, format="PNG")
    ocr = retrieval_ocr.LocalOcr(str(tmp_path))
    assert parameters["dtype"] == "bfloat16"
    assert ocr.read(content.getvalue()) == "Local OCR evidence"
    assert ocr.read(content.getvalue()) == "Local OCR evidence"
    assert loaded == ["download", "load"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))


@pytest.mark.anyio
async def test_remote_relations_survive_failed_local_entity_proposals() -> None:
    from eru.documents import Entity
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    class Knowledge:
        async def entities(
            self, text: str, policy: ExtractionPolicy | None = None
        ) -> tuple[Entity, ...]:
            raise RuntimeError("private entity payload")

    def remote(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                TextPart(
                    content='{"facts":[{"subject":"Alice","subject_type":"PERSON","predicate":"visits","object":"Paris","object_type":"LOCATION","evidence":"Alice visits Paris"}]}'
                )
            ]
        )

    result = await DocumentRuntime(
        knowledge=Knowledge(), relation_model=FunctionModel(remote)
    ).extract(
        ExtractFacts(
            passages=(ExtractPassage(id="a" * 64, text="Alice visits Paris"),)
        )
    )
    assert result.passages[0].facts[0].object == "Paris"
    assert result.warnings == ("entity_extraction_skipped",)


@pytest.mark.anyio
@pytest.mark.parametrize("status", (401, 403, 429, 500))
async def test_relation_provider_failures_are_classified_without_leaking_payloads(
    status: int,
) -> None:
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def remote(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(
            status_code=status,
            model_name="fixture",
            body={"secret": "private payload"},
        )

    result = await DocumentRuntime(
        relation_model=FunctionModel(remote)
    ).extract(
        ExtractFacts(
            passages=(ExtractPassage(id="a" * 64, text="Alice visits Paris"),)
        )
    )
    assert "graph_extraction_unavailable" in result.warnings
    assert ("graph_extraction_unauthorized" in result.warnings) == (
        status in {401, 403}
    )
    assert "private payload" not in result.model_dump_json()


@pytest.mark.anyio
async def test_slow_entity_proposals_do_not_block_remote_relations_or_queue_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from eru.documents import Entity
    from galadril_scribe import retrieval_runtime
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    release = asyncio.Event()
    calls: list[str] = []

    class Knowledge:
        async def entities(
            self, text: str, policy: ExtractionPolicy | None = None
        ) -> tuple[Entity, ...]:
            calls.append(text)
            await release.wait()
            return ()

    def remote(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                TextPart(
                    content='{"facts":[{"subject":"Alice","subject_type":"PERSON","predicate":"visits","object":"Paris","object_type":"LOCATION","evidence":"Alice visits Paris"}]}'
                )
            ]
        )

    monkeypatch.setattr(
        retrieval_runtime, "_ENTITY_PROPOSAL_TIMEOUT", 0.01, raising=False
    )
    runtime = DocumentRuntime(
        knowledge=Knowledge(), relation_model=FunctionModel(remote)
    )
    request = ExtractFacts(
        passages=(ExtractPassage(id="a" * 64, text="Alice visits Paris"),)
    )
    try:
        first = await asyncio.wait_for(runtime.extract(request), timeout=1)
        second = await asyncio.wait_for(runtime.extract(request), timeout=1)
        assert calls == ["Alice visits Paris"]
        assert first.passages[0].facts and second.passages[0].facts
        assert (
            first.warnings == second.warnings == ("entity_extraction_skipped",)
        )
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_images_without_ocr_text_still_produce_a_searchable_unit() -> (
    None
):
    import io

    from galadril_scribe.retrieval_models import PdfParser
    from PIL import Image

    class EmptyOcr:
        def read(self, image: bytes) -> str:
            return ""

    content = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(content, format="PNG")
    units = await PdfParser(ocr=EmptyOcr()).parse(
        content.getvalue(), "image/png"
    )
    assert len(units) == 1
    assert units[0].text == "[Image]"


def test_visual_embedding_batches_reject_misaligned_and_query_images() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EmbedTexts(texts=("cat", "dog"), images=("aGVsbG8=",))
    with pytest.raises(ValidationError):
        EmbedTexts(texts=("cat",), query=True, images=("aGVsbG8=",))


@pytest.mark.anyio
async def test_visual_vectors_are_not_reused_as_text_only_memory() -> None:
    class Embedder:
        async def embed(self, request: EmbedTexts) -> EmbeddedTexts:
            return EmbeddedTexts(profile_id="a" * 64, vectors=((1.0, 0.0),))

    runtime = DocumentRuntime(embedder=Embedder())
    await runtime.embed(EmbedTexts(texts=("P",), images=("aGVsbG8=",)))
    assert not runtime._memory_vectors
    await runtime.embed(EmbedTexts(texts=("P",)))
    assert ("a" * 64, "P") in runtime._memory_vectors


@pytest.mark.anyio
async def test_text_only_reranking_cannot_erase_visual_relevance() -> None:
    class Reranker:
        profile_id = "text-only"

        async def scores(
            self, query: str, documents: tuple[str, ...]
        ) -> tuple[float, ...]:
            return tuple(0.0 for _ in documents)

    result = await DocumentRuntime(reranker=Reranker()).rank(
        RankPassages(
            query="chat",
            min_relevance=0.3,
            passages=(
                Passage(id="cat", text="P", dense_score=0.8, visual=True),
                Passage(id="text", text="irrelevant", dense_score=0.8),
            ),
        )
    )
    assert [(hit.id, hit.score) for hit in result.passages] == [("cat", 0.8)]


@pytest.mark.anyio
async def test_embedding_api_accepts_bounded_visual_payloads_larger_than_text_budget() -> (
    None
):
    import httpx
    from fastapi import FastAPI
    from galadril_scribe.retrieval_api import install_documents

    class Embedder:
        async def embed(self, request: EmbedTexts) -> EmbeddedTexts:
            assert request.texts == ("P",)
            assert request.images == ("a" * 2_100_000,)
            return EmbeddedTexts(profile_id="a" * 64, vectors=((1.0, 0.0),))

    app = FastAPI()
    install_documents(app, DocumentRuntime(embedder=Embedder()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://scribe"
    ) as client:
        response = await client.post(
            "/documents/embed",
            json={"texts": ["P"], "images": ["a" * 2_100_000]},
        )
    assert response.status_code == 200
    assert response.json()["vectors"] == [[1.0, 0.0]]
