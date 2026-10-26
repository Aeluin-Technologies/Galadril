//! Pipeline evidence keeps chronology, source identity and query bounds
//! explicit.

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EvidenceFilter {
    pub from_ms: i64,
    pub until_ms: i64,
    pub sender: Option<String>,
    pub recipient: Option<String>,
    pub thread_id: Option<String>,
    pub document_id: Option<String>,
    pub ontology_id: Option<String>,
}

impl EvidenceFilter {
    pub fn validated(mut self) -> Result<Self> {
        ensure!(
            self.from_ms >= 0 &&
                self.until_ms > self.from_ms &&
                self.until_ms - self.from_ms <= 100 * 366 * 86400000_i64,
            "Invalid evidence period"
        );
        for text in [
            &mut self.sender,
            &mut self.recipient,
            &mut self.thread_id,
            &mut self.document_id,
            &mut self.ontology_id,
        ]
        .into_iter()
        .flatten()
        {
            ensure!(
                !text.trim().is_empty() &&
                    text.len() <= 998 &&
                    !text.chars().any(char::is_control),
                "Invalid evidence filter"
            );
            *text = text.trim().into();
        }
        for address in [&mut self.sender, &mut self.recipient]
            .into_iter()
            .flatten()
        {
            ensure!(
                address.len() <= 320 &&
                    address.matches('@').count() == 1 &&
                    address.split_once('@').is_some_and(
                        |(local, domain)| !local.is_empty() &&
                            domain.contains('.') &&
                            !domain.ends_with('.')
                    ) &&
                    address.bytes().all(|b| b.is_ascii_graphic()),
                "Invalid evidence address"
            );
            address.make_ascii_lowercase();
        }
        Ok(self)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EvidenceFact {
    pub subject: String,
    pub predicate: String,
    pub object: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct EvidenceRow {
    pub source_id: String,
    pub event_id: String,
    pub event_time_ms: i64,
    pub unit_id: String,
    pub title: String,
    pub text: String,
    pub metadata: Value,
    pub facts: Vec<EvidenceFact>,
    #[serde(default)]
    pub score: f64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EvidencePage {
    pub items: Vec<EvidenceRow>,
    pub after: Option<String>,
    pub engine: String,
    pub warnings: Vec<String>,
}
