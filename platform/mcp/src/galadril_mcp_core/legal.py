"""Share LibreJustice request constraints between Pengolo and the MCP service."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Solution = Literal[
    "REJET",
    "IRRECEVABILITE",
    "DESISTEMENT",
    "NON_LIEU_A_STATUER",
    "CONFIRMATION",
    "INFIRMATION",
    "INFIRMATION_PARTIELLE",
    "REFORMATION",
    "CASSATION",
    "CASSATION_PARTIELLE",
    "ANNULATION",
    "CONFORMITE",
    "NON_CONFORMITE",
    "INELIGIBILITE",
    "SATISFACTION_TOTALE",
    "SATISFACTION_PARTIELLE",
    "AUTRE",
]
Filter = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]


class DecisionSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    query: str = Field(default="", max_length=512)
    solution: tuple[Solution, ...] = Field(default=(), max_length=17)
    seat: tuple[Filter, ...] = Field(default=(), max_length=20)
    formation: tuple[Filter, ...] = Field(default=(), max_length=20)
    publication: tuple[Filter, ...] = Field(default=(), max_length=20)
    legal_domain: tuple[Filter, ...] = Field(default=(), max_length=20)
    legal_instrument: tuple[Filter, ...] = Field(default=(), max_length=20)
    legal_article: tuple[Filter, ...] = Field(default=(), max_length=20)
    search_in: tuple[Literal["headnote", "judgment", "annexes"], ...] = Field(
        default=(), max_length=3
    )
    date_from: date | None = None
    date_to: date | None = None
    mode: Literal["auto", "lexical", "semantic", "reference"] = "auto"
    sort: Literal["relevance", "date_desc", "date_asc"] = "relevance"
    limit: int = Field(default=20, ge=1, le=20)
    ai_rerank: bool | None = None

    @model_validator(mode="after")
    def validate_search(self) -> Self:
        """Reject incompatible settings before the query leaves Galadril."""
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must precede or equal date_to")
        if self.mode == "semantic" and self.sort != "relevance":
            raise ValueError("A date sort requires a non-semantic search mode")
        if not self.query.strip() and not (
            self.solution
            or self.seat
            or self.formation
            or self.publication
            or self.legal_domain
            or self.legal_instrument
            or self.legal_article
            or self.date_from
            or self.date_to
        ):
            raise ValueError(
                "Provide a public query or narrow the corpus with a filter"
            )
        return self


class NormSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    query: str = Field(default="", max_length=512)
    code: Filter | None = None
    jurisdiction: Filter | None = None
    as_of: date | None = None
    valid_from: date | None = None
    limit: int = Field(default=10, ge=1, le=20)


class NormRead(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = Field(default=None, min_length=1, max_length=2048)
    text_id: Filter | None = None
    code: Filter | None = None
    article: Filter | None = None
    as_of: date | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        """Bind the date to one unambiguous public source address."""
        if (
            sum(
                value is not None
                for value in (self.url, self.text_id, self.code)
            )
            != 1
        ):
            raise ValueError("Provide exactly one of url, text_id or code")
        if self.article is not None and self.url is not None:
            raise ValueError("Include the article in the URL when using url")
        return self
