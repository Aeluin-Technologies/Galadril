"""Only model computation is replayed; production parsing and graph ranking remain live."""

import httpx
import uvloop
from galadril_scribe.contracts import Settings
from galadril_scribe.main import serve
from galadril_scribe.retrieval_contracts import (
    ExtractedFacts,
    ExtractFacts,
    PassageFacts,
    RecognizedFacts,
    RecognizeFacts,
)
from galadril_scribe.retrieval_models import PdfParser
from galadril_scribe.retrieval_runtime import DocumentRuntime
from galadril_scribe.runtime import Runtime
from galadril_scribe.sandbox import Sandbox
from retrieval_benchmark import CachedEmbedder, CachedReranker, load_benchmark


class FixtureDocuments(DocumentRuntime):
    __slots__ = ("data",)

    def __init__(self) -> None:
        self.data = load_benchmark()
        super().__init__(
            embedder=CachedEmbedder(self.data),
            reranker=CachedReranker(self.data),
            parser=PdfParser(),
        )

    async def extract(self, request: ExtractFacts) -> ExtractedFacts:
        documents = {
            document.text: document for document in self.data.documents
        }
        return ExtractedFacts(
            passages=tuple(
                PassageFacts(
                    id=item.id,
                    facts=documents[item.text].facts
                    if item.text in documents
                    else (),
                )
                for item in request.passages
            )
        )

    async def recognize(self, request: RecognizeFacts) -> RecognizedFacts:
        try:
            facts = {
                fact.model_dump_json()
                for fact in self.data.recognized(request.query)
            }
        except StopIteration:
            return RecognizedFacts(warnings=("recognition_unavailable",))
        return RecognizedFacts(
            indices=tuple(
                index
                for index, item in enumerate(request.facts)
                if item.fact.model_dump_json() in facts
            )[:5]
        )


def factory(
    settings: Settings, client: httpx.AsyncClient, sandbox: Sandbox
) -> Runtime:
    runtime = Runtime(settings, client, sandbox=sandbox)
    runtime.documents = FixtureDocuments()
    return runtime


if __name__ == "__main__":
    uvloop.run(serve(runtime_factory=factory))
