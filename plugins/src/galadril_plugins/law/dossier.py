"""Validate a complete administrative litigation dossier before reuse."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal, cast

from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from galadril_plugins.registry import PluginScope
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

type Identifier = Annotated[
    str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
]
type Label = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]
type Section = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8192)
]


class Item(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    id: Identifier


class Party(Item):
    name: Label
    public_postal_address: Label | None = None


class Proceeding(Item):
    court: Label
    court_address: Label | None = None
    number: Label | None = None
    regime: Literal[
        "fond",
        "exces_pouvoir",
        "plein_contentieux",
        "refere_suspension",
        "refere_liberte",
        "refere_mesures_utiles",
        "refere_modification",
        "appel",
        "cassation",
        "autre",
    ]
    defendant_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=16)
    claimant_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)


class ProceedingLink(BaseModel):
    model_config = Item.model_config
    source_id: Identifier
    target_id: Identifier
    kind: Literal["suspends", "appeals", "related", "joined"]


class Exhibit(Item):
    title: Label
    source_ref: Identifier


class ProceduralEvent(Item):
    proceeding_id: Identifier
    kind: Literal[
        "depot",
        "notification",
        "communication",
        "cloture",
        "audience",
        "decision",
        "jonction",
    ]
    occurred_at: AwareDatetime | None = None
    source_ref: Identifier | None = None
    exhibit_ids: tuple[Identifier, ...] = Field(default=(), max_length=64)


class Filing(Item):
    proceeding_id: Identifier
    filed_on: date | None = None
    exhibit_ids: tuple[Identifier, ...] = Field(default=(), max_length=64)


class Petition(Filing):
    subject: Section
    facts: Section
    discussion: Section
    relief: Section


class Brief(Filing):
    kind: Literal[
        "complementaire",
        "replique",
        "defense",
        "recapitulatif",
        "observations",
        "autre",
    ]
    body: Section
    responds_to: Identifier | None = None


def _ids(items: Sequence[Item]) -> set[str]:
    identifiers = {item.id for item in items}
    if len(identifiers) != len(items):
        raise ValueError("Duplicate dossier identifier")
    return identifiers


class Dossier(Item):
    parties: tuple[Party, ...] = Field(min_length=1, max_length=32)
    proceedings: tuple[Proceeding, ...] = Field(min_length=1, max_length=16)
    petition: Petition
    briefs: tuple[Brief, ...] = Field(default=(), max_length=64)
    exhibits: tuple[Exhibit, ...] = Field(default=(), max_length=64)
    links: tuple[ProceedingLink, ...] = Field(default=(), max_length=64)
    events: tuple[ProceduralEvent, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def linked(self) -> Dossier:
        parties = _ids(self.parties)
        proceedings = _ids(self.proceedings)
        exhibits = _ids(self.exhibits)
        _ids(self.events)
        filings = (self.petition, *self.briefs)
        _ids(filings)
        for proceeding in self.proceedings:
            roles = (*proceeding.defendant_ids, *proceeding.claimant_ids)
            if len(set(roles)) != len(roles) or not parties.issuperset(roles):
                raise ValueError("Invalid party in proceeding")
        previous: dict[str, str] = {}
        for filing in filings:
            if (
                filing.proceeding_id not in proceedings
                or not exhibits.issuperset(filing.exhibit_ids)
                or len(set(filing.exhibit_ids)) != len(filing.exhibit_ids)
            ):
                raise ValueError("Unknown proceeding or exhibit in filing")
            if isinstance(filing, Brief) and filing.responds_to is not None:
                if previous.get(filing.responds_to) != filing.proceeding_id:
                    raise ValueError(
                        "Response must cite an earlier filing in the same proceeding"
                    )
            previous[filing.id] = filing.proceeding_id
        for event in self.events:
            if (
                event.proceeding_id not in proceedings
                or not exhibits.issuperset(event.exhibit_ids)
            ):
                raise ValueError(
                    "Unknown proceeding or exhibit in procedural event"
                )
        regimes = {
            proceeding.id: proceeding.regime for proceeding in self.proceedings
        }
        seen: set[tuple[str, str, str]] = set()
        for link in self.links:
            endpoints = (link.source_id, link.target_id)
            if link.source_id == link.target_id or not proceedings.issuperset(
                endpoints
            ):
                raise ValueError("Invalid proceeding relationship")
            if link.kind == "suspends" and (
                regimes[link.source_id] != "refere_suspension"
                or regimes[link.target_id]
                not in {
                    "fond",
                    "exces_pouvoir",
                    "plein_contentieux",
                    "appel",
                    "cassation",
                }
            ):
                raise ValueError(
                    "Suspension requires a related merits proceeding"
                )
            source, target = (
                sorted(endpoints)
                if link.kind in {"related", "joined"}
                else endpoints
            )
            key = (link.kind, source, target)
            if key in seen:
                raise ValueError("Duplicate proceeding relationship")
            seen.add(key)
        return self

    def wire(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class DossierBlock:
    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        dossier = Dossier.model_validate(command.payload.get("law_dossier"))
        return {**command.payload, "law_dossier": dossier.wire()}


def dossier_block(scope: PluginScope, step: PipelineStep) -> DossierBlock:
    return DossierBlock()
