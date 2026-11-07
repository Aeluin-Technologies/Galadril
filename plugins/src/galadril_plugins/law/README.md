# Law

Le plugin fournit une ontologie du contentieux français. Le modèle LaTeX et
les spécialistes Pengolo couvrent la branche administrative.

## Modèle de document

Dans Pengolo, demander la liste des modèles de documents et choisir
**Contentieux administratif — recoursadmin**. La génération PDF et l'export
du projet sont disponibles si le compilateur de documents est déployé et
si l'administration du tenant a activé les modèles du plugin `law`.

1. Pour un rendu local, remplacer les marqueurs de `information.tex` par les coordonnées du dossier.
2. Choisir l'acte dans `draft.tex` avec `\input{memoire}`, `\input{requete}`
   ou `\input{courrier}`.
3. Remplacer les champs entre crochets et relire le PDF. Le dossier d'exemple
   utilise le recours pour excès de pouvoir. Pour un autre régime, modifier
   le troisième argument de `\DeclarerDossier` et vérifier la qualification
   affichée.

Déclarer les pièces avec `\DeclarerPiece{numéro}{intitulé}` ou
`\AjouterPiece{intitulé}`. Les citations autonomes, les renvois courts et
le bordereau utilisent respectivement `\CitationPiece`, `\RefPiece` et
`\BordereauPieces`.

Déclarer les décisions vérifiées avec
`\DeclarerArret{clé}{juridiction}{date}{numéro}[url=…,ecli=…]`.
Les commandes `\Arret` et `\ArretParenthese` acceptent un repère facultatif,
par exemple `\Arret[pt.~12]{clé}`. La forme étoilée désactive le lien pour
cette citation. `\FicheArret` affiche aussi l'ECLI lorsqu'il est renseigné.

Les sources éditables se trouvent sous `latex/`. Après modification,
reconstruire la ressource distribuée depuis la racine du dépôt.

```sh
uv run plugins/src/galadril_plugins/law/package_template.py
bazel test //plugins/tests:document_templates_test
```

## État de la confidentialité

Les contextes du plugin exigent une entrée pseudonymisée. Cette exigence est
déclarée dans le manifeste et contrôlée par les contrats génériques de Gateway
et Scribe. Tant qu’une préparation complète et approuvée n’est pas disponible,
les générations et les chargements privés sont refusés avant transmission au
modèle. Le caviardage manuel natif produit une copie à relire, sans attestation
d’anonymisation complète ; il ne débloque pas automatiquement ces contextes.
L’extraction Télérecours reste à implémenter.

### Remplissage dans Pengolo

Dans un projet de contentieux administratif, le modèle publié `recoursadmin`
utilise les coordonnées de la juridiction et de l’administration enregistrées
pour ce projet. Pengolo demande les champs requis qui manquent puis présente
leurs valeurs pour confirmation avant enregistrement. Le profil du requérant
se renseigne séparément dans **Personnalisation → Profil privé pour les documents**.

Gateway chiffre ce profil par utilisateur et par tenant. Le compilateur
récupère les champs juste avant le rendu et les échappe dans `information.tex`.
Le modèle publié, ses exports de source et les tâches de compilation
contiennent seulement les marqueurs ou les données de rédaction. Les PDF
produits depuis un projet spécialisé sont déposés dans la bibliothèque
personnelle de l’utilisateur. Leur contenu est exclu de l’index et de la
lecture par les outils du chat.

Le déploiement doit activer le plugin de modèles `law` et le service de
compilation. Fournir à Gateway `PENGOLO_DOCUMENT_VAULT_KEY` sous la forme de
32 octets aléatoires encodés en base64. Fournir une clé aléatoire distincte de
64 caractères hexadécimaux dans `DOCUMENT_COMPILER_BINDING_KEY` à Gateway
et au compilateur uniquement. Conserver la clé du coffre pour relire les
profils existants. Pour compiler les sources manuellement, remplacer les
marqueurs de `information.tex` localement.

## Spécialistes Pengolo

Les skills sont classés sous `skills/administratif/`, `skills/judiciaire/`
et `skills/penal/`. Les neuf skills administratifs conservent leurs
identifiants. Les branches civile et pénale sont réservées à de futurs skills.

- Coordination du dossier
- Faits et pièces
- Recevabilité et procédure
- Recherche de jurisprudence
- Veille juridique
- Revue indépendante de la défense
- Rédaction contentieuse
- Vérification des citations
- Relecture avant validation

Créer dans Pengolo un projet de la branche voulue et sélectionner la carte
de l’équipe juridique compatible. Déplier la carte permet de consulter ses
spécialistes. La branche est fixée à la création. Chaque
projet juridique a sa propre bibliothèque. Les conversations et les agents
personnalisés d’un dossier ne se déplacent pas vers un autre projet. Pour une
première analyse, l’équipe réunit notamment Faits et pièces, Recherche de
jurisprudence et Revue indépendante de la défense. Activer la recherche Web
lorsqu'elle est configurée et nécessaire. Les consignes de recherche
prévoient des requêtes publiques dépourvues de données du dossier et la
vérification du texte des décisions dans les bases officielles.
[ArianeWeb](https://www.conseil-etat.fr/arianeweb/),
[Données ouvertes de la justice administrative](https://opendata.justice-administrative.fr/),
[Légifrance](https://www.legifrance.gouv.fr/codes/texte_lc/LEGITEXT000006070933/).

Scribe lance jusqu’à huit spécialistes en parallèle. L’orchestrateur peut
poser jusqu’à deux questions de suivi ciblées, à deux spécialistes distincts,
si le budget de génération le permet. Il peut utiliser les outils autorisés
entre les sections de sa réponse.

L’agent de défense reçoit uniquement les pièces jointes du message que
l’utilisateur choisit expressément de lui communiquer. Sa mission est celle
du skill officiel. Il ne reçoit ni le message, ni l’historique, ni les
préférences, ni les rapports des autres agents. Il n’a aucun outil. Son
rapport est réservé à la synthèse. Lorsque les relances sont possibles,
l’orchestrateur clôt les consultations avant de lire ce rapport.

La rédaction demande de confirmer les coordonnées publiques trouvées à partir
du nom ou de l'adresse d'un organisme. Les informations personnelles restent
des champs à renseigner dans les sources hors du fil. La saisie dédiée, le
profil chiffré réutilisable et leur liaison au compilateur restent à intégrer.
[Annuaire de l'administration et des services publics](https://www.data.gouv.fr/dataservices/api-annuaire-de-ladministration-et-des-services-publics).

## Télérecours Citoyens

Le futur connecteur devra relever les événements d'un compte autorisé et
rattacher les pièces et mémoires à leur dossier. Avant de choisir son
protocole, il reste à établir les flux d'authentification, les identifiants
stables, les règles de reprise et la provenance des documents. Le mécanisme
d'ingestion autorisée de Gateway constitue le point d'intégration côté
Galadril. [Contrat des connecteurs](../../../README.md#authorized-input-delivery).

## Ontologie du dossier

[Le paquet `law.contentieux`](contentieux-ontology.json) fournit un socle
commun aux contentieux français. Registry matérialise une vue du paquet pour
le tenant et publie des révisions immuables. L’instance, le mémoire, les
écritures, les pièces, les assertions, les moyens, les décisions et leur
dispositif gardent des identifiants partagés. Le mémoire s’emploie aussi,
par exemple, en cassation civile et devant la chambre de l’instruction.
[CPC, art. 978](https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000029729587)
· [CPP, art. 198](https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000051743075).

Pour un dossier, choisir le paquet de sa branche dans le catalogue. Les
paquets `law.administratif`, `law.judiciaire` et `law.penal` contiennent le
socle applicable et leur seule branche. Les références vers les ressources
écartées sont exclues. `law.contentieux` conserve le catalogue historique
complet et les révisions existantes.

Les trois branches décrivent des pratiques au sein des deux ordres
juridictionnels français. Le civil et le pénal partagent le type de
juridiction `law.court.judicial`. La dénomination officielle de la
juridiction, sa catégorie, la formation saisie et le rôle qu’elle exerce
dans l’instance sont distincts.
[Ministère de la Justice, organisation des cours et tribunaux](https://www.justice.gouv.fr/justice-france/cours-tribunaux).

| Branche | Juridictions et particularités |
| --- | --- |
| `law.administratif` | Tribunal administratif, cour administrative d’appel, Conseil d’État, Cour nationale du droit d’asile. Les actes administratifs, les recours préalables et le référé-suspension restent représentés dans leurs modules historiques. [Conseil d’État, les tribunaux et cours](https://www.conseil-etat.fr/site/qui-sommes-nous/les-tribunaux-et-cours). |
| `law.judiciaire` | Matière civile, commerciale ou sociale. Le tribunal judiciaire, le tribunal de commerce et le conseil de prud’hommes relèvent de l’ordre judiciaire. L’assignation et les conclusions écrites spécialisent l’écriture commune. La requête introductive peut aussi servir en matière civile. [Juridictions civiles](https://www.justice.gouv.fr/justice-france/lorganisation-cours-tribunaux/lordre-judiciaire/juridictions-civiles) · [CPC, art. 54](https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000042597347) · [CPC, art. 768](https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000039623422). |
| `law.penal` | Formations et juridictions pénales, dont le tribunal correctionnel, le tribunal de police, la cour criminelle départementale et la cour d’assises. Le chef de poursuite conserve la personne visée et les qualifications attribuées. Les prétentions civiles et les peines prononcées ont leurs propres types. [Juridictions pénales](https://www.justice.gouv.fr/justice-france/lorganisation-cours-tribunaux/lordre-judiciaire/juridictions-penales) · [CPP, du tribunal correctionnel](https://www.legifrance.gouv.fr/codes/id/LEGISCTA000006151898). |

Le paquet fournit des libellés de référence en anglais, avec les termes
techniques français conservés. Le catalogue du dashboard fournit les
libellés français sans modifier les définitions ni les noms saisis par
les tenants.

| Module | Ce qu’il représente |
| --- | --- |
| `law.procedure` | Dossier, instances, parties et juridiction. La catégorie de contentieux, la procédure, le degré, la fonction exercée par le juge et le ressort sont des axes distincts. |
| `law.ecritures` | Requêtes et mémoires rattachés à leur instance de dépôt. Le dossier et chaque instance peuvent réunir plusieurs écritures. |
| `law.evenements` | Événements matériels ou procéduraux datables. L’instant de l’événement et celui de sa consignation sont distincts. |
| `law.actes` | Actes et recours administratifs préalables. Un document peut porter, notifier, publier ou seulement révéler un acte. Adoption, publication, notification et entrée en vigueur sont des événements distincts. |
| `law.faits` | Événements matériels, assertions attribuées et appréciations probatoires successives. Le statut allégué, contesté ou établi est celui d’une appréciation sourcée. |
| `law.normes` | Sources, dispositions et versions temporelles, règles formulées, applicabilité contextualisée, relations de hiérarchie ou de dérogation et interprétations tirées des motifs du juge. |
| `law.moyens` | Conclusions de partie, causes juridiques, moyens, arguments, objections et réponses. Une identité stable relie leurs formulations successives, chacune rattachée à une écriture et à ses passages. |
| `law.preuves` | Pièces, productions et cotes par bordereau, versions de documents, captures de provenance et passages précis. Une relation probatoire attribuée qualifie l’étayage ou la contradiction d’une assertion. |
| `law.decisions` | Motifs, constatations du juge, traitement des moyens et des conclusions, articles ordonnés du dispositif, effets prononcés et portée. Voies mentionnées, voies exercées et clôtures sont identifiées séparément. |
| `law.delais` | Faits générateurs, fondements, interruptions, suspensions et prorogations, puis appréciations successives de l’échéance et de son opposabilité. |
| `law.execution` | Injonctions, obligations, astreintes, liquidations, constats d’exécution et instances d’exécution. |

### Dispositif et chaîne probatoire

Un dossier clôturé pointe vers les constats de clôture de ses instances.
Chaque constat renvoie, lorsqu’il est identifié, au dispositif de la décision
qui termine cette instance. Chaque article du dispositif conserve son rang et
son passage source. Il peut porter plusieurs effets ou dispositions, par
exemple l’annulation partielle d’un acte, une injonction de réexamen et le
rejet du surplus. Le sort de chaque conclusion pointe vers les articles
pertinents. Le sort d’un moyen pointe vers la décision et ses motifs ou
passages pertinents, y compris lorsqu’un moyen est expressément laissé sans
examen. Les observations sur les recours et le caractère exécutoire sont
indépendantes des constats de clôture. Une exécution partielle peut donc
être consignée après la clôture de l’instance.

Dans un dossier fictif, une pièce numérisée contient une lettre qui révèle
une décision administrative. Le passage exact de la lettre est rattaché à
sa version et à sa provenance. Une partie affirme avoir reçu la lettre
à une certaine date. Un passage d’un accusé de réception peut étayer ou
contredire cette assertion. La qualification de la notification mobilise
ensuite une règle dont les versions et le champ d’application sont
identifiés. Le moyen invoque cette qualification, l’adversaire objecte que
l’envoi et la réception sont distincts, et le juge consigne son appréciation
dans un motif. Si le dispositif ordonne un réexamen, l’obligation et ses
constats d’exécution se rattachent à cet effet précis.

La demande de suspension relève de `law.suspension_request`, qui peut
pointer vers l’instance au fond. La suspension prononcée relève de
`law.suspension_effect` et procède d’un article du dispositif. Les événements
et appréciations de délai restent sourcés, y compris lorsqu’une notification
est connue et que l’opposabilité reste à vérifier.
[CJA, pouvoirs du juge des référés](https://www.legifrance.gouv.fr/codes/section_lc/LEGITEXT000006070933/LEGISCTA000006150399/?anchor=LEGIARTI000006449326).
[CJA, délais](https://www.legifrance.gouv.fr/codes/section_lc/LEGITEXT000006070933/LEGISCTA000006136478/).
[CJA, exécution](https://www.legifrance.gouv.fr/codes/id/LEGISCTA000006118216).

### Cardinalités, dates et accès

- `min_count` et `max_count` s’appliquent du point de vue de la source.
  L’absence de maximum indique un nombre non borné. Les champs
  `inverse_min_count` et `inverse_max_count` bornent le nombre de sources
  pour certaines relations de composition. `inverse_of` désigne une
  projection inverse, filtrée aux requêtes dans `law.proceeding.petitions`.
  `required` décrit les propriétés attendues sur un objet complet. Une
  extraction incomplète conserve ses sources et ses champs manquants.
- Les intervalles `valid_from`–`valid_until` sont semi-ouverts. Une borne
  absente laisse sa valeur indéterminée. Les dates d’effet, de capture et
  d’appréciation sont conservées séparément. Une révision d’un document
  ou d’une disposition reçoit une nouvelle identité de version. Les liens
  vers les passages et versions antérieures restent attachés à leur
  source d’origine.
- `access_scope` documente la frontière attendue. Les ressources du dossier
  sont soumises à ses droits. Les documents et passages héritent des droits
  de leurs sources. Seule une publication vérifiée peut fournir une
  projection publique distincte. Une référence opaque, une citation ou
  un lien avec une source publique ne confère aucun droit. Gateway et les
  services propriétaires restent responsables de l’autorisation du dossier,
  des documents et du tenant. Le profil nominatif chiffré relève d’un
  parcours séparé.
- Registry valide la cohérence de la définition, dont les références,
  l’héritage et les extrémités des liens. Les nouvelles cardinalités,
  indications d’accès et règles de cohérence entre occurrences décrivent le
  contrat des futurs producteurs. Elles ne constituent pas un contrôle
  d’accès ni une validation des instances à elles seules. Le paquet
  n’infère ni fait établi, ni opposabilité, ni échéance, ni résultat du
  litige. Les appréciations, y compris celles proposées par un modèle,
  gardent leur origine et leur état de vérification.

### Compatibilité et spécialisations

La version 0.3 conserve les modules, les identifiants de ressources et le
contrat de `law.algorithm.dossier`. Le validateur et le
rendu LaTeX continuent d’accepter le format historique, avec sa requête
introductive désignée. Dans le nouveau modèle, `law.filing.proceeding`
reste le rattachement canonique d’une écriture et
`law.proceeding.petitions` expose l’ensemble des requêtes de l’instance.
Les anciens champs et liens restent lisibles comme projections de
compatibilité. Les révisions déjà publiées et leurs données restent
rattachées à leur version d’origine.

Dans le socle, la requête désignée et le régime administratif historique
sont facultatifs. Le dossier administratif historique conserve ses
exigences dans son validateur. La branche administrative exige au moins
une partie défenderesse. Les liens de branche vers la juridiction et vers
les défendeurs administratifs décrivent les mêmes relations que les liens
génériques, avec des contraintes plus précises. Les producteurs doivent
maintenir la cohérence de ces projections. Aucune catégorie de juridiction
ne détermine à elle seule la compétence ou l’issue d’une instance.

Les extensions réservent leurs propres préfixes et déclarent les dépendances
dans une définition Registry fermée. Par exemple,
`law.university.exam_regulation` peut spécialiser `law.administrative_act`
et ajouter ses propres liens vers les formations et les épreuves.
`law.urbanisme` et `law.public_contracts` peuvent ajouter des types de la
même manière. Les extensions reprennent les relations du socle pour les
versions, les moyens, les preuves et les décisions, et publient leurs
propres révisions.
