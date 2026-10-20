//! Document evidence and vector profiles remain distinct from ESKG entities.

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum IndexStage {
    Reading,
    Parsing,
    Extracting,
    Embedding,
    Publishing,
}

#[derive(
    Debug, Clone, Copy, Default, Deserialize, Serialize, PartialEq, Eq,
)]
#[serde(rename_all = "snake_case")]
pub enum SearchMode {
    #[default]
    Hybrid,
    Lexical,
    Semantic,
    Hipporag2,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SearchInput {
    pub query: String,
    #[serde(default)]
    pub project_id: Option<String>,
    #[serde(default)]
    pub file_ids: Vec<String>,
    #[serde(default)]
    pub mime_types: Vec<String>,
    #[serde(default)]
    pub updated_after: Option<i64>,
    #[serde(default)]
    pub updated_before: Option<i64>,
    #[serde(default)]
    pub mode: SearchMode,
    #[serde(default = "default_limit")]
    pub limit: usize,
    #[serde(default)]
    pub min_similarity: Option<f64>,
    #[serde(default)]
    pub min_relevance: f64,
    #[serde(default = "include_eskg")]
    pub include_eskg: bool,
}

const fn include_eskg() -> bool {
    true
}

const fn default_limit() -> usize {
    8
}

fn identifier(value: &str, length: usize) -> bool {
    value.len() == length &&
        value.bytes().all(|byte| {
            byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
        })
}

impl SearchInput {
    pub fn parse(value: Value) -> Result<Self> {
        let input: Self = serde_json::from_value(value)?;
        ensure!(
            !input.query.trim().is_empty() && input.query.len() <= 2048,
            "Invalid document query"
        );
        ensure!(
            (1..=20).contains(&input.limit) &&
                input.file_ids.len() <= 128 &&
                input.mime_types.len() <= 16,
            "Document query exceeds limit"
        );
        ensure!(
            input
                .project_id
                .as_ref()
                .is_none_or(|id| identifier(id, 32)) &&
                input.file_ids.iter().all(|id| identifier(id, 32)),
            "Invalid document scope"
        );
        ensure!(
            input
                .mime_types
                .iter()
                .all(|mime| mime.len() <= 128 && mime.contains('/')),
            "Invalid document MIME filter"
        );
        ensure!(
            input.min_similarity.is_none_or(
                |score| score.is_finite() && (-1.0..=1.0).contains(&score)
            ) && input.min_relevance.is_finite() &&
                (0.0..=1.0).contains(&input.min_relevance),
            "Invalid document relevance threshold"
        );
        ensure!(
            input
                .updated_after
                .zip(input.updated_before)
                .is_none_or(|(first, last)| first <= last),
            "Invalid document date filter"
        );
        Ok(input)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fact {
    pub subject: String,
    pub predicate: String,
    pub object: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Unit {
    pub id: String,
    pub ordinal: i32,
    pub text: String,
    pub start: i64,
    pub end: i64,
    pub page: Option<i32>,
    #[serde(default)]
    pub facts: Vec<Fact>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Feature {
    pub unit_id: String,
    pub ordinal: i32,
    pub embedding: Vec<f32>,
}

pub fn validate_index(
    source: &str,
    units: &[Unit],
    features: &[Feature],
) -> Result<()> {
    ensure!(
        identifier(source, 32) &&
            !units.is_empty() &&
            units.len() <= 32 &&
            features.len() <= 288,
        "Indexing batch exceeds limit"
    );
    let mut ids = std::collections::HashSet::with_capacity(units.len());
    for unit in units {
        ensure!(
            identifier(&unit.id, 64) &&
                ids.insert(unit.id.as_str()) &&
                (0..2048).contains(&unit.ordinal),
            "Invalid evidence identifier"
        );
        ensure!(
            !unit.text.trim().is_empty() &&
                unit.text.len() <= 16384 &&
                unit.start >= 0 &&
                unit.end.checked_sub(unit.start) ==
                    i64::try_from(unit.text.chars().count()).ok() &&
                unit.page.is_none_or(|page| (1..=1000).contains(&page)),
            "Invalid evidence span"
        );
        ensure!(
            unit.facts.len() <= 8 &&
                unit.facts.iter().all(|fact| [
                    &fact.subject,
                    &fact.predicate,
                    &fact.object
                ]
                .iter()
                .all(|text| !text.trim().is_empty() && text.len() <= 1024)),
            "Invalid extracted relationships"
        );
    }
    let mut dimension = None;
    let mut positions =
        std::collections::HashSet::with_capacity(features.len());
    for feature in features {
        let unit = units.iter().find(|unit| unit.id == feature.unit_id);
        ensure!(
            unit.is_some() &&
                positions
                    .insert((feature.unit_id.as_str(), feature.ordinal)) &&
                (feature.ordinal == -1 ||
                    usize::try_from(feature.ordinal)
                        .ok()
                        .is_some_and(|ordinal| unit.is_some_and(
                            |unit| ordinal < unit.facts.len()
                        ))),
            "Vector has no matching evidence"
        );
        let size = feature.embedding.len();
        ensure!(
            (1..=4096).contains(&size) &&
                dimension.is_none_or(|expected| expected == size),
            "Incompatible vector dimensions"
        );
        dimension = Some(size);
        let norm: f64 = feature
            .embedding
            .iter()
            .map(|value| f64::from(*value).powi(2))
            .sum();
        ensure!(
            feature.embedding.iter().all(|value| value.is_finite()) &&
                norm.is_finite() &&
                norm > 1e-12,
            "Invalid evidence vector"
        );
    }
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SearchHit {
    pub file: crate::domain::pengolo::Record,
    pub source_id: String,
    pub source_checksum: String,
    pub unit_id: String,
    pub filename: String,
    pub mime: String,
    pub text: String,
    pub page: Option<i32>,
    pub start: i64,
    pub end: i64,
    pub similarity: Option<f64>,
    pub relevance: Option<f64>,
    pub rank_score: f64,
    pub graph_score: Option<f64>,
    pub facts: Vec<Fact>,
    #[serde(default)]
    pub eskg_refs: Vec<GraphReference>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct GraphReference {
    pub entity_id: String,
    pub event_id: String,
    pub state_type: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SearchPage {
    pub hits: Vec<SearchHit>,
    pub warnings: Vec<String>,
    pub lexical_backend: String,
    pub embedding_profile: Option<String>,
    pub reranker_profile: Option<String>,
    pub engine: String,
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn search_scope_cannot_supply_tenant_or_expand_its_source_filters()
    -> anyhow::Result<()> {
        let valid = SearchInput::parse(
            json!({"query":"irrégularité", "mode":"hipporag2", "limit":8,"minSimilarity":0.4}),
        )?;
        assert_eq!(valid.limit, 8);
        for invalid in [
            json!({"query":"x","tenant":"another"}),
            json!({"query":"x","limit":1000}),
            json!({"query":"x","fileIds":["../secret"]}),
            json!({"query":"x","minSimilarity":2.0}),
            json!({"query":" "}),
        ] {
            assert!(SearchInput::parse(invalid).is_err());
        }
        Ok(())
    }

    #[test]
    fn indexed_vectors_require_one_finite_nonzero_dimension_and_exact_provenance()
    -> anyhow::Result<()> {
        let source = "a".repeat(32);
        let units = vec![Unit {
            id: "b".repeat(64),
            ordinal: 0,
            text: "proof".into(),
            start: 0,
            end: 5,
            page: Some(2),
            facts: Vec::new(),
        }];
        let mut features = vec![Feature {
            unit_id: "b".repeat(64),
            ordinal: -1,
            embedding: vec![1.0, 0.0],
        }];
        validate_index(&source, &units, &features)?;
        features
            .first_mut()
            .ok_or_else(|| anyhow::anyhow!("Missing fixture"))?
            .embedding = vec![f32::NAN, 0.0];
        assert!(validate_index(&source, &units, &features).is_err());
        features
            .first_mut()
            .ok_or_else(|| anyhow::anyhow!("Missing fixture"))?
            .embedding = vec![0.0, 0.0];
        assert!(validate_index(&source, &units, &features).is_err());
        Ok(())
    }
}
