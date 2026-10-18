//! Navigation descriptors identify safe destinations without carrying
//! executable authority.

use std::collections::{BTreeMap, HashSet};
use std::sync::LazyLock;

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

#[derive(
    Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq,
)]
#[serde(rename_all = "snake_case")]
pub enum NavigationKind {
    #[default]
    Navigation,
    Action,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationEntry {
    pub id: String,
    pub href: String,
    pub labels: BTreeMap<String, String>,
    pub descriptions: BTreeMap<String, String>,
    pub aliases: Vec<String>,
    #[serde(default)]
    pub administrator: bool,
    #[serde(default)]
    pub kind: NavigationKind,
}

pub struct NavigationCatalog {
    entries: Vec<IndexedNavigation>,
}

struct IndexedNavigation {
    descriptor: NavigationEntry,
    phrases: Vec<Vec<String>>,
}

fn words(value: &str) -> Vec<String> {
    let normalized: String = value
        .chars()
        .flat_map(char::to_lowercase)
        .filter(|character| !('\u{0300}'..='\u{036f}').contains(character))
        .map(|character| match character {
            'à' | 'â' | 'ä' => 'a',
            'é' | 'è' | 'ê' | 'ë' => 'e',
            'î' | 'ï' => 'i',
            'ô' | 'ö' => 'o',
            'ù' | 'û' | 'ü' => 'u',
            'ç' => 'c',
            character if character.is_alphanumeric() => character,
            _ => ' ',
        })
        .collect();
    normalized
        .split_whitespace()
        .filter_map(|word| {
            match word {
                "a" | "an" | "the" | "of" | "to" | "and" | "de" | "des" |
                "du" | "la" | "le" | "les" | "un" | "une" | "et" | "au" |
                "aux" | "pour" | "d" | "l" => None,
                "gerer" | "gestion" | "administrer" | "administer" |
                "manage" | "managing" | "management" | "configurer" |
                "configure" => Some("manage"),
                "user" | "users" | "utilisateur" | "utilisateurs" |
                "membre" | "membres" | "people" | "personne" |
                "personnes" | "member" | "members" => Some("people"),
                "role" | "roles" => Some("role"),
                "creer" | "creation" | "ajouter" | "ajout" | "create" |
                "new" | "nouveau" | "nouvelle" | "add" => Some("create"),
                "document" | "documents" | "fichier" | "fichiers" |
                "file" | "files" => Some("document"),
                word => Some(word),
            }
            .map(str::to_owned)
        })
        .collect()
}

impl NavigationCatalog {
    /// Shares one immutable catalog rather than allocating a copy per tenant.
    pub fn builtin() -> Result<&'static Self> {
        static CATALOG: LazyLock<Result<NavigationCatalog>> = LazyLock::new(
            || {
                NavigationCatalog::from_json(include_str!(
                    "../../../../front/dashboard/app/utils/navigation-catalog.json"
                ))
            },
        );
        CATALOG.as_ref().map_err(|error| {
            anyhow::anyhow!("Invalid navigation catalog: {error}")
        })
    }

    /// Rejects redirects and ambiguous descriptors before exposing
    /// destinations.
    pub fn from_json(source: &str) -> Result<Self> {
        ensure!(
            source.len() <= 128 * 1024,
            "Navigation catalog is too large"
        );
        let entries: Vec<NavigationEntry> = serde_json::from_str(source)?;
        ensure!(entries.len() <= 256, "Too many navigation entries");
        let mut ids = HashSet::with_capacity(entries.len());
        for entry in &entries {
            ensure!(
                !entry.id.is_empty() &&
                    entry.id.len() <= 128 &&
                    entry
                        .id
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() ||
                            byte == b'-') &&
                    ids.insert(&entry.id),
                "Invalid or duplicate navigation identity"
            );
            ensure!(
                entry.href.starts_with('/') &&
                    !entry.href.starts_with("//") &&
                    entry.href.len() <= 256 &&
                    entry
                        .href
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() ||
                            b"/-_?#=&".contains(&byte)),
                "Invalid navigation destination"
            );
            ensure!(
                !entry.href.starts_with("/settings") || entry.administrator,
                "Unprotected administration destination"
            );
            for localized in [&entry.labels, &entry.descriptions] {
                ensure!(
                    localized.len() <= 16,
                    "Too many navigation translations"
                );
                for locale in ["en", "fr"] {
                    let text = localized
                        .get(locale)
                        .context("Missing navigation translation")?;
                    ensure!(
                        !text.trim().is_empty() &&
                            text.len() <= 512 &&
                            !text.chars().any(char::is_control),
                        "Invalid navigation translation"
                    );
                }
            }
            ensure!(
                entry.aliases.len() <= 32 &&
                    entry.aliases.iter().all(|alias| !alias
                        .trim()
                        .is_empty() &&
                        alias.len() <= 256 &&
                        !alias.chars().any(char::is_control)),
                "Invalid navigation aliases"
            );
        }
        Ok(Self {
            entries: entries
                .into_iter()
                .map(|descriptor| {
                    let phrases = descriptor
                        .labels
                        .values()
                        .chain(&descriptor.aliases)
                        .chain(descriptor.descriptions.values())
                        .map(|text| words(text))
                        .collect();
                    IndexedNavigation {
                        descriptor,
                        phrases,
                    }
                })
                .collect(),
        })
    }

    /// Visibility is evaluated for the current identity, never cached as
    /// authority.
    pub fn visible(
        &self,
        administrator: bool,
    ) -> impl Iterator<Item = &NavigationEntry> {
        self.entries
            .iter()
            .map(|entry| &entry.descriptor)
            .filter(move |entry| !entry.administrator || administrator)
    }

    /// Searches precomputed terms without inference or per-user catalog
    /// copies.
    pub fn search(
        &self,
        query: &str,
        administrator: bool,
    ) -> Vec<(&NavigationEntry, i32)> {
        if query.len() > 512 {
            return Vec::new();
        }
        let tokens = words(query);
        if tokens.is_empty() {
            return Vec::new();
        }
        let mut found = Vec::with_capacity(12);
        for entry in &self.entries {
            if entry.descriptor.administrator && !administrator {
                continue;
            }
            let score =
                entry
                    .phrases
                    .iter()
                    .filter(|phrase| {
                        tokens.iter().all(|token| {
                            phrase.iter().any(|word| {
                                word == token ||
                                    (token.len() >= 2 &&
                                        word.starts_with(token))
                            })
                        })
                    })
                    .map(|phrase| {
                        let exact =
                            tokens.iter().all(|token| phrase.contains(token));
                        i32::try_from(phrase.len().abs_diff(tokens.len()))
                            .map_or(0, |difference| {
                                1000 - difference * 10 -
                                    if exact { 0 } else { 100 }
                            })
                    })
                    .max();
            if let Some(score) = score &&
                score > 0
            {
                found.push((&entry.descriptor, score));
            }
        }
        found.sort_unstable_by(|(left, left_score), (right, right_score)| {
            right_score
                .cmp(left_score)
                .then_with(|| left.id.cmp(&right.id))
        });
        found.truncate(12);
        found
    }
}

#[cfg(test)]
mod tests {
    use anyhow::{Context, Result};

    use super::*;

    #[test]
    fn search_matches_bilingual_intents_and_bounds_the_shortlist() -> Result<()>
    {
        let catalog = NavigationCatalog::builtin()?;
        for (query, expected) in [
            ("RÔLE", "permissions"),
            ("ajouter un rôle", "create-role"),
            ("cre\u{0301}er un ro\u{0302}le", "create-role"),
            ("users manage", "people"),
            ("gestion des utilisateurs", "people"),
        ] {
            let hits = catalog.search(query, true);
            assert_eq!(
                hits.first().map(|(entry, _)| entry.id.as_str()),
                Some(expected)
            );
        }
        assert!(catalog.search("role", false).is_empty());
        assert!(catalog.search("de la the", true).is_empty());
        assert!(catalog.search(&"x".repeat(513), true).is_empty());
        assert!(catalog.search("unknown-destination", true).is_empty());
        Ok(())
    }

    #[test]
    fn builtin_catalog_has_stable_destinations_and_filters_administrative_entries()
    -> Result<()> {
        let catalog = NavigationCatalog::builtin()?;
        let visible: Vec<_> = catalog.visible(false).collect();
        anyhow::ensure!(
            visible.iter().any(|entry| entry.id == "documents"),
            "Missing document navigation"
        );
        assert!(visible.iter().all(|entry| !entry.administrator &&
            !entry.href.starts_with("/settings")));
        let action = catalog
            .visible(true)
            .find(|entry| entry.id == "create-role")
            .context("Missing role action")?;
        assert_eq!(action.href, "/settings/sso?task=create-role#permissions");
        assert_eq!(action.kind, NavigationKind::Action);
        Ok(())
    }

    #[test]
    fn catalog_rejects_redirects_duplicate_ids_and_unprotected_administrative_destinations()
    -> Result<()> {
        let catalog = NavigationCatalog::builtin()?;
        let entry = catalog
            .visible(true)
            .next()
            .context("Empty navigation catalog")?;
        for href in [
            "//attacker.invalid",
            "https://attacker.invalid",
            "/\\attacker.invalid",
            "/%2fattacker.invalid",
            "javascript:alert(1)",
        ] {
            let mut value = serde_json::to_value(entry)?;
            value
                .as_object_mut()
                .context("Invalid descriptor")?
                .insert("href".into(), serde_json::json!(href));
            assert!(
                NavigationCatalog::from_json(
                    &serde_json::json!([value]).to_string()
                )
                .is_err()
            );
        }
        assert!(
            NavigationCatalog::from_json(
                &serde_json::json!([entry, entry]).to_string()
            )
            .is_err()
        );
        let mut value = serde_json::to_value(entry)?;
        value
            .as_object_mut()
            .context("Invalid descriptor")?
            .insert("administrator".into(), serde_json::json!(false));
        assert!(
            NavigationCatalog::from_json(
                &serde_json::json!([value]).to_string()
            )
            .is_err()
        );
        Ok(())
    }
}
