"""Entity extraction backends for Eru."""

from eru.extractor.entity_merger import DefaultEntityMerger
from eru.extractor.gliner import GlinerExtractor
from eru.extractor.structured import StructuredCandidateExtractor

__all__ = [
    "DefaultEntityMerger",
    "GlinerExtractor",
    "StructuredCandidateExtractor",
]
