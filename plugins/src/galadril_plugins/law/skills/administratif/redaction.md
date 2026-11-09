# Rédaction contentieuse

Rédige un projet de requête, de mémoire ou de courrier au greffe selon la
demande. Associe chaque moyen à la règle applicable, au fait démontré, à
la pièce et à la conséquence demandée. Dans un mémoire en réplique, réponds
aux arguments effectivement soulevés dans les écritures fournies.

Si l'instance, la juridiction ou l'administration en cause manque, demande
les références publiques utiles. Un nom ou une adresse publique suffit pour
lancer une recherche si le Web est disponible. Consulte en priorité
[l'annuaire officiel de l'administration](https://lannuaire.service-public.gouv.fr/)
et le site de l'organisme. Présente le nom officiel, l'adresse postale et le
lien de chaque résultat plausible. Demande une confirmation explicite avant
d'insérer ces coordonnées, y compris lorsqu'il n'y a qu'un résultat. Distingue
la correspondance d'une adresse de la compétence de la juridiction.

Laisse le nom, le prénom et l'adresse personnelle du demandeur à renseigner
hors de la conversation. Limite la recherche Web aux coordonnées publiques
de la juridiction ou de l'administration.

Pour une requête ou un mémoire issu d'un dossier complet, valide d'abord
le dossier avec law.algorithm.dossier. Maintiens la distinction entre
l'objet, les faits, la discussion et les conclusions de la requête.
Sélectionne l'écriture et l'instance correspondantes avant de produire le
LaTeX. Le rendu de l'écriture charge information.tex pour les informations
à renseigner en dehors du fil.

Pour produire un PDF dans Pengolo, appelle document_templates avec list et
sélectionne le modèle publié recoursadmin. Appelle document_templates avec
document_case et son template_id. Si des champs du dossier manquent, pose les
questions nécessaires avec ask_user, puis soumets les réponses publiques
avec document_case et fields. L’outil demande leur confirmation avant de les enregistrer. Demande uniquement les
champs signalés, notamment le nom, l'adresse et le représentant de
l'administration ainsi que la juridiction et l'adresse du greffe. Les clés
communiquées sont sans le préfixe project_. Le numéro de dossier est facultatif.

Si profile_ready est faux, indique où renseigner le profil privé dans les
réglages de personnalisation. Ne demande jamais ces coordonnées dans le chat.
Dans source pour compile_document, passe seulement le corps LaTeX de l’acte
destiné à draft.tex, avec les commandes NatureActe et les blocs begin et end
document. Le modèle publié charge la classe et information.tex. Adapte le
corps au courrier, au mémoire ou à la requête demandé. Le compilateur remplit
les variables project_ et profile_ et les échappe. Ne fournis pas ces
variables dans la requête de compilation. Utilise uniquement les autorités
vérifiées et présente le résultat comme un projet à relire.

## Composition et révision

Utilise cette compétence uniquement pour composer, réviser ou produire les
écritures, après le travail de qualification. La compétence de raisonnement
et de vocabulaire s'applique aussi ici. Présente les conclusions précises,
principales, subsidiaires et accessoires, puis les moyens qui les soutiennent.
Fais apparaître les questions préalables et les causes juridiques pertinentes.
Resserre les mémoires ultérieurs sur les points encore débattus et signale les
nouveautés. Numérote continûment les pièces d'un mémoire à l'autre; cite le
passage utile plutôt que reproduire des pièces ou de longues décisions.
Le guide TA/CAA recommande une écriture claire et proportionnée; il n'impose
pas par lui-même une condition de recevabilité nouvelle.

Sur demande de document, ne te limite pas à afficher du texte dans le chat :
utilise le modèle recoursadmin et le compilateur existants. Une impossibilité
technique doit être explicitée, sans prétendre avoir produit un PDF.
N'utilise ni plugin_catalog ni enable_plugin pour découvrir un modèle depuis
un chat spécialisé : list et read suffisent; l'administration des modèles
appartient aux réglages. La compilation et le dépôt restent deux opérations
distinctes; soumets toujours le projet à validation humaine.

## Date du document

Renseigner et faire confirmer la date explicite de signature dans le champ public
`document_date` du dossier. Le modèle la lie à `\DateDepot` dans `information.tex`.
Ne pas utiliser `\today` pour dater une écriture : l’horloge reproductible du
compilateur est indépendante de la date du document. Si cette date manque, la
demander avant la compilation ; ne pas inventer une date de dépôt.
