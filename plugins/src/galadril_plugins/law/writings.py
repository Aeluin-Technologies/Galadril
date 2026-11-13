"""Render verified filing fields as literal LaTeX in the public template."""

from galadril_plugins.law.dossier import Dossier, Petition

_LATEX = str.maketrans(
    {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "%": r"\%",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "_": r"\_",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
        "’": "'",
        "‘": "`",
        "“": "``",
        "”": "''",
        "–": "--",
        "—": "---",
        "\u00a0": "~",
        "\u202f": r"\,",
    }
)
_REGIMES = {
    "exces_pouvoir": "fond",
    "refere_suspension": "suspension",
    "refere_liberte": "liberte",
    "refere_mesures_utiles": "mesures-utiles",
    "refere_modification": "modification",
}
_OTHER_REGIMES = {
    "fond": "Recours au fond",
    "plein_contentieux": "Plein contentieux",
    "appel": "Appel",
    "cassation": "Cassation",
    "autre": "Contentieux administratif",
}


def render_filing(dossier: Dossier, filing_id: str) -> str:
    """Select a verified filing and leave claimant details in the separate input."""
    filing = next(
        (
            item
            for item in (dossier.petition, *dossier.briefs)
            if item.id == filing_id
        ),
        None,
    )
    if filing is None:
        raise ValueError("Unknown litigation filing")
    proceeding = next(
        item for item in dossier.proceedings if item.id == filing.proceeding_id
    )
    court = proceeding.court.translate(_LATEX)
    court_address = (proceeding.court_address or "").translate(_LATEX)
    number = (proceeding.number or "").translate(_LATEX)
    mode = _REGIMES.get(proceeding.regime, "autre")
    parts = [
        r"\documentclass{recoursadmin}",
        r"\GaladrilEnableSignatureFields",
        r"\input{information}",
        rf"\DefinirJuridiction{{{court}}}{{{court}}}{{{court_address}}}",
        rf"\DeclarerDossier{{redaction}}{{{number}}}{{{mode}}}",
        r"\ChoisirDossier{redaction}",
    ]
    if mode == "autre":
        parts.append(
            rf"\QualificationEntete{{{_OTHER_REGIMES[proceeding.regime]}}}"
        )
    defendant_ids = set(proceeding.defendant_ids)
    defendants = [
        party for party in dossier.parties if party.id in defendant_ids
    ]
    names = ", ".join(party.name.translate(_LATEX) for party in defendants)
    address = (
        (defendants[0].public_postal_address or "").translate(_LATEX)
        if len(defendants) == 1
        else ""
    )
    parts.append(rf"\DefinirDefendeur{{{names}}}{{}}{{{address}}}{{}}")
    if isinstance(filing, Petition):
        parts.extend(
            [
                r"\NatureActe{requete}",
                rf"\ObjetActe{{{filing.subject.translate(_LATEX)}}}",
            ]
        )
        if mode == "autre":
            parts.append(r"\TitreDocumentLibre{Requête}")
        body = [
            r"\TitrePartie{Faits et procédure}",
            filing.facts.translate(_LATEX),
            "",
            r"\TitrePartie{Discussion}",
            filing.discussion.translate(_LATEX),
            "",
            r"\ParCesMotifs",
            filing.relief.translate(_LATEX),
        ]
    else:
        parts.append(r"\NatureActe{memoire}")
        if filing.kind == "autre":
            parts.append(r"\TitreDocumentLibre{Mémoire}")
        else:
            parts.append(rf"\TypeMemoire{{{filing.kind}}}")
        body = [filing.body.translate(_LATEX)]
    if filing.exhibit_ids:
        cited = set(filing.exhibit_ids)
        for position, exhibit in enumerate(dossier.exhibits, start=1):
            if exhibit.id not in cited:
                continue
            parts.append(
                rf"\DeclarerPiece{{{position}}}{{{exhibit.title.translate(_LATEX)}}}"
            )
    parts.extend(
        [r"\begin{document}", r"\EnteteDocument", *body, r"\SignatureRequerant"]
    )
    if filing.exhibit_ids:
        parts.extend([r"\clearpage", r"\BordereauPieces"])
    parts.append(r"\end{document}")
    return "\n".join(parts) + "\n"
