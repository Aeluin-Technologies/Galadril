"""Pin source attribution and review boundaries with synthetic legal fixtures."""

import unittest

from galadril_plugins.law.instruction import (
    Decomposition,
    Ground,
    IncidenceCheck,
)
from pydantic import ValidationError


class InstructionTest(unittest.TestCase):
    def test_decomposition_preserves_original_passages_and_valid_dependencies(
        self,
    ) -> None:
        ground = Ground(
            id="m1",
            question="La compétence est-elle établie ?",
            cause="legalite_externe",
            branches=("incompetence",),
            passages=("L'auteur n'avait pas délégation.",),
            evidence=(0,),
        )
        result = Decomposition(grounds=(ground,))
        dependent = ground.model_copy(
            update={"id": "m2", "depends_on": ("m1",)}
        )
        independent = ground.model_copy(update={"id": "m3"})
        self.assertEqual(
            [
                [item.id for item in layer]
                for layer in Decomposition(
                    grounds=(dependent, ground, independent)
                ).layers()
            ],
            [["m1", "m3"], ["m2"]],
        )
        result.verify_source("L'auteur n'avait pas délégation.", 1)
        with self.assertRaises(ValueError):
            result.verify_source("Autre contenu.", 1)
        with self.assertRaises(ValueError):
            result.verify_source("L'auteur n'avait pas délégation.", 0)
        with self.assertRaises(ValidationError):
            Decomposition(
                grounds=(
                    ground.model_copy(update={"depends_on": ("missing",)}),
                )
            )

    def test_danthony_does_not_apply_to_competence_or_unverified_premises(
        self,
    ) -> None:
        for nature in ("competence", "fond", "procedure_juridictionnelle"):
            with self.assertRaises(ValidationError):
                IncidenceCheck(
                    mechanism="danthony",
                    vice=nature,
                    scope_verified=True,
                    premise_refs=("piece1:p2",),
                )
        with self.assertRaises(ValidationError):
            IncidenceCheck(
                mechanism="danthony",
                vice="procedure_administrative",
                scope_verified=False,
                premise_refs=(),
            )
        check = IncidenceCheck(
            mechanism="danthony",
            vice="procedure_administrative",
            scope_verified=True,
            premise_refs=("piece1:p2",),
        )
        self.assertEqual(check.status, "human_review_required")
        for mechanism, vice in (
            ("el_bahi", "motif"),
            ("hallal", "base_legale"),
        ):
            with (
                self.subTest(mechanism=mechanism),
                self.assertRaises(ValidationError),
            ):
                IncidenceCheck(
                    mechanism=mechanism,
                    vice=vice,
                    scope_verified=True,
                    premise_refs=("piece1:p2",),
                )
        for mechanism, vice in (
            ("el_bahi", "base_legale"),
            ("hallal", "motif"),
        ):
            self.assertEqual(
                IncidenceCheck(
                    mechanism=mechanism,
                    vice=vice,
                    scope_verified=True,
                    premise_refs=("piece1:p2",),
                ).status,
                "human_review_required",
            )


if __name__ == "__main__":
    unittest.main()
