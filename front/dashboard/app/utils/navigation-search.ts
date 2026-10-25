/** Explicit bilingual intents keep navigation local and prevent model-generated actions. */
import entries from "./navigation-catalog.json" with { type: "json" };
import { record } from "#shared/records.ts";

interface NavigationContext {
  locale?: string;
  administrator?: unknown;
}
interface NavigationEntry {
  id: string;
  kind: "navigation" | "action";
  href: string;
  label: string;
  description: string;
  score: number;
}

const ignored = new Set([
  "a",
  "an",
  "the",
  "of",
  "to",
  "and",
  "de",
  "des",
  "du",
  "la",
  "le",
  "les",
  "un",
  "une",
  "et",
  "au",
  "aux",
  "pour",
  "d",
  "l",
]);
const concepts = new Map<string, string>(
  (
    [
      [
        "manage",
        [
          "gerer",
          "gestion",
          "administrer",
          "administer",
          "manage",
          "managing",
          "management",
          "configurer",
          "configure",
        ],
      ],
      [
        "people",
        [
          "user",
          "users",
          "utilisateur",
          "utilisateurs",
          "membre",
          "membres",
          "people",
          "personne",
          "personnes",
          "member",
          "members",
        ],
      ],
      ["role", ["role", "roles"]],
      [
        "create",
        [
          "creer",
          "creation",
          "ajouter",
          "ajout",
          "create",
          "new",
          "nouveau",
          "nouvelle",
          "add",
        ],
      ],
      [
        "document",
        ["document", "documents", "fichier", "fichiers", "file", "files"],
      ],
    ] satisfies [string, string[]][]
  ).flatMap(([concept, aliases]) =>
    aliases.map((alias): [string, string] => [alias, concept]),
  ),
);

function words(value: string) {
  return (
    value
      .normalize("NFKD")
      .replace(/\p{Diacritic}/gu, "")
      .toLowerCase()
      .match(/[\p{Letter}\p{Number}]+/gu) || []
  )
    .filter((word) => !ignored.has(word))
    .map((word) => concepts.get(word) || word);
}

const catalog = entries.map((entry) => ({
  ...entry,
  phrases: [
    ...Object.values(entry.labels),
    ...entry.aliases,
    ...Object.values(entry.descriptions),
  ].map(words),
}));

export function navigationEntries(context: NavigationContext = {}) {
  const locale = context.locale?.startsWith("fr") ? "fr" : "en";
  return catalog
    .filter((entry) => !entry.administrator || context.administrator === true)
    .map((entry) => ({
      id: entry.id,
      kind: entry.kind || "navigation",
      href: entry.href,
      label: entry.labels[locale],
      description: entry.descriptions[locale],
    }));
}

/** Stable identities preserve the immediate local shortcut when remote matches arrive. */
export function mergeNavigation(
  local: unknown[],
  remote: unknown[],
): Omit<NavigationEntry, "score">[] {
  const seen = new Set();
  return [...local, ...remote]
    .flatMap<Omit<NavigationEntry, "score">>((value) => {
      const entry = record(value);
      if (
        !entry ||
        typeof entry.id !== "string" ||
        !/^[A-Za-z0-9-]{1,128}$/.test(entry.id) ||
        seen.has(entry.id) ||
        typeof entry.href !== "string" ||
        entry.href.startsWith("//") ||
        !/^\/[A-Za-z0-9/_?#=&-]{0,255}$/.test(entry.href) ||
        typeof entry.kind !== "string" ||
        !["navigation", "action"].includes(entry.kind) ||
        typeof entry.label !== "string" ||
        entry.label.length > 512 ||
        typeof entry.description !== "string" ||
        entry.description.length > 512
      )
        return [];
      seen.add(entry.id);
      return [
        {
          id: entry.id,
          kind: entry.kind === "action" ? "action" : "navigation",
          href: entry.href,
          label: entry.label,
          description: entry.description,
        },
      ];
    })
    .slice(0, 12);
}

/** Reordered words and prefixes match explicit intents without executing any action. */
export function searchNavigation(
  query: unknown,
  context: NavigationContext = {},
) {
  if (typeof query !== "string" || query.length > 512) return [];
  const tokens = words(query);
  if (!tokens.length) return [];
  const locale = context.locale?.startsWith("fr") ? "fr" : "en";
  const found: NavigationEntry[] = [];
  for (const entry of catalog) {
    if (entry.administrator && context.administrator !== true) continue;
    let score = 0;
    for (const phrase of entry.phrases) {
      if (
        !tokens.every((token) =>
          phrase.some(
            (word) =>
              word === token || (token.length >= 2 && word.startsWith(token)),
          ),
        )
      )
        continue;
      const exact = tokens.every((token) => phrase.includes(token));
      score = Math.max(
        score,
        1000 - Math.abs(phrase.length - tokens.length) * 10 - (exact ? 0 : 100),
      );
    }
    if (score > 0)
      found.push({
        id: entry.id,
        kind: entry.kind === "action" ? "action" : "navigation",
        href: entry.href,
        label: entry.labels[locale],
        description: entry.descriptions[locale],
        score,
      });
  }
  return found
    .sort(
      (left, right) =>
        right.score - left.score || left.id.localeCompare(right.id),
    )
    .slice(0, 12);
}
