"""Architecture constraints for the TerminusDB-backed ontology package."""

from importlib.util import find_spec

from galadril_ontology.backends.terminus import (
    TerminusClient,
    TerminusOntologyRepository,
)


def test_terminus_backend_has_one_public_import_boundary() -> None:
    assert TerminusClient.__module__.endswith(".client")
    assert TerminusOntologyRepository.__module__.endswith(".repository")


def test_removed_postgres_ontology_modules_are_not_importable() -> None:
    assert find_spec("galadril_ontology.postgres") is None
    assert find_spec("galadril_ontology.schema") is None
