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
des champs à renseigner dans les sources hors du fil. La saisie dédiée et le profil chiffré sont reliés au compilateur comme décrit
ci-dessus.
[Annuaire de l'administration et des services publics](https://www.data.gouv.fr/dataservices/api-annuaire-de-ladministration-et-des-services-publics).

## Télérecours Citoyens

Le futur connecteur devra relever les événements d'un compte autorisé et
rattacher les pièces et mémoires à leur dossier. Avant de choisir son
protocole, il reste à établir les flux d'authentification, les identifiants
stables, les règles de reprise et la provenance des documents. Le mécanisme
d'ingestion autorisée de Gateway constitue le point d'intégration côté
Galadril. [Contrat des connecteurs](../../../README.md#authorized-input-delivery).

## Ontologies par couches

Le catalogue 0.4.0 sépare les définitions communes des trois branches. Chaque
couche importe une version exacte de ses dépendances, dans le même plugin.

| Paquet              | Contenu propre                                                                                                        |
| ------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `law.socle`         | Dossier, instance, juridiction, partie, jugement rendu et article.                                                    |
| `law.ecritures`     | Une écriture avec son type : requête, mémoire en défense, en réplique ou complémentaire ; son auteur et son instance. |
| `law.moyens`        | Moyens reliés aux écritures, aux articles et aux jugements cités.                                                     |
| `law.preuves`       | Pièces, numérotation, document source et référence de fichier.                                                        |
| `law.contentieux`   | Composition des quatre couches et contrat d’extraction du dossier.                                                    |
| `law.administratif` | Instance et juridiction administratives, acte attaqué et document qui le révèle.                                      |
| `law.judiciaire`    | Instance et juridiction civiles, commerciales ou sociales.                                                            |
| `law.penal`         | Instance et juridiction pénales.                                                                                      |

Le dossier relie les instances, les parties et les écritures. Une instance en
cours conserve son numéro et sa procédure ; elle ne constitue pas un jugement.
Le jugement rendu conserve son dispositif et sa source. Une partie comporte
un nom, une adresse et un mode de représentation ; son représentant peut être
une autre partie. Les informations personnelles du requérant destinées au
rendu LaTeX restent dans le coffre privé décrit ci-dessus.

Les liens structurés permettent de remonter du moyen à l’écriture, aux articles,
aux jurisprudences et aux pièces citées. Les cardinalités sont déclarées sur les
propriétés et les liens ; elles décrivent le contrat attendu des producteurs.
Une définition d’ontologie ne remplace pas l’autorisation ni la validation des
occurrences. Aucune qualification ou conclusion juridique n’est inférée par ces
paquets.

Registry reçoit une définition fermée après résolution des imports. Les couches
ne créent aucun accès à une autre ontologie ou à un autre tenant. Leur provenance
est conservée ; le graphe du dashboard peut les replier en blocs et les déplier.
Les libellés officiels sont traduits par le frontend ; les termes techniques du
droit français et les noms saisis par l’utilisateur restent inchangés.

L’historisation, les états temporels et les événements relèvent des mécanismes
natifs de Vision, de l’ESKG et de Timescale. Le catalogue ne duplique plus ces
mécanismes par des objets « version d’objection », « version de moyen » ou par
une chronologie juridique parallèle.

### Compatibilité

Les révisions Registry déjà publiées restent immuables et leurs données restent
liées à leur schéma d’origine. La version 0.4.0 est un nouveau schéma simplifié,
sans migration destructive automatique. Ses identifiants conservés gardent leur
identité ; les anciens objets de version restent consultables dans les anciennes
révisions. Toute migration des occurrences exige un mapping explicite et revu.
Le validateur administratif historique de `law.algorithm.dossier` et le modèle
LaTeX restent compatibles avec leur format d’entrée existant. Les spécialistes
administratifs restent séparés des branches judiciaire et pénale.
