//! Outbound port for retrieving graph relations for an entity.

use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct GraphNode {
    pub id: String,
    pub kind: GraphNodeKind,
    pub ontology_ref: Option<OntologyReference>,
    pub properties: Value,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GraphNodeKind {
    Entity,
    Event,
    State,
    LatentIdentity,
    Inference,
    Decision,
    CausalVariable,
    Metric,
}

impl GraphNodeKind {
    /// Rejects business classifications at the instance-graph boundary.
    pub fn from_label(label: &str) -> Result<Self> {
        match label {
            "Entity" => Ok(Self::Entity),
            "Event" => Ok(Self::Event),
            "State" => Ok(Self::State),
            "LatentIdentity" => Ok(Self::LatentIdentity),
            "Inference" => Ok(Self::Inference),
            "Decision" => Ok(Self::Decision),
            "CausalVariable" => Ok(Self::CausalVariable),
            "Metric" => Ok(Self::Metric),
            _ => bail!("Unsupported structural graph kind: {label}"),
        }
    }

    /// Returns the stable AGE label and public API structural kind.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Entity => "Entity",
            Self::Event => "Event",
            Self::State => "State",
            Self::LatentIdentity => "LatentIdentity",
            Self::Inference => "Inference",
            Self::Decision => "Decision",
            Self::CausalVariable => "CausalVariable",
            Self::Metric => "Metric",
        }
    }

    /// Defines which ontology classifications can apply to an instance role.
    pub const fn ontology_kind(self) -> Option<&'static str> {
        match self {
            Self::Entity | Self::LatentIdentity => Some("object_type"),
            Self::Event => Some("event_type"),
            Self::State | Self::CausalVariable => Some("property"),
            Self::Inference | Self::Decision | Self::Metric => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OntologyReference {
    pub tenant_id: String,
    pub ontology_id: String,
    pub revision_id: String,
    pub resource_id: String,
    pub resource_kind: String,
}

impl OntologyReference {
    /// Checks wire provenance without resolving or rewriting Registry state.
    pub fn validate(
        &self,
        tenant_id: &str,
        kind: GraphNodeKind,
    ) -> Result<()> {
        if self.tenant_id != tenant_id ||
            self.ontology_id.is_empty() ||
            self.ontology_id.len() > 128 ||
            !valid_resource_id(&self.resource_id) ||
            !(20..=128).contains(&self.revision_id.len()) ||
            !self.revision_id.bytes().all(|b| {
                b.is_ascii_alphanumeric() || b == b'_' || b == b'-'
            }) ||
            kind.ontology_kind() != Some(self.resource_kind.as_str())
        {
            bail!("Invalid versioned ontology reference for graph node");
        }
        Ok(())
    }
}

/// Matches Registry's stable dotted resource identities without allocations.
fn valid_resource_id(value: &str) -> bool {
    let mut count = 0;
    for segment in value.split('.') {
        let mut bytes = segment.bytes();
        if !bytes.next().is_some_and(|b| b.is_ascii_lowercase()) ||
            !bytes.all(|b| {
                b.is_ascii_lowercase() ||
                    b.is_ascii_digit() ||
                    b == b'_' ||
                    b == b'-'
            })
        {
            return false;
        }
        count += 1;
    }
    count >= 2
}

use GraphNodeKind::{
    CausalVariable, Decision, Entity, Event, Inference, LatentIdentity,
    Metric, State,
};

pub const RELATION_ENDPOINTS: &[(&str, GraphNodeKind, GraphNodeKind)] = &[
    ("TRIGGERS", State, Event),
    ("LEADS_TO", Event, State),
    ("EVOLUTION", State, State),
    ("CONTAIN", Event, Event),
    ("OCCUR", Event, Entity),
    ("INFLUENCE", Event, Entity),
    ("HAS_INFERENCE", Event, Inference),
    ("CONSIDERED_CANDIDATE", Inference, Entity),
    ("CONSIDERED_CANDIDATE", Inference, LatentIdentity),
    ("HAS_DECISION", Inference, Decision),
    ("SELECTED_TARGET", Decision, Entity),
    ("SELECTED_TARGET", Decision, LatentIdentity),
    ("PROMOTED_AS", LatentIdentity, Entity),
    ("MERGED_WITH", LatentIdentity, LatentIdentity),
    ("SUPERSEDES", Event, Event),
    ("SUPERSEDES", State, State),
    ("SUPERSEDES", Inference, Inference),
    ("SUPERSEDES", Decision, Decision),
    ("SUPERSEDES", LatentIdentity, LatentIdentity),
    ("REVOKES", Event, Event),
    ("REVOKES", State, State),
    ("REVOKES", Inference, Inference),
    ("REVOKES", Decision, Decision),
    ("REVOKES", LatentIdentity, LatentIdentity),
    ("STATE_OF", State, Entity),
    ("APPEARS_IN", Entity, Event),
    ("PARTICIPATED_IN", Entity, Event),
    ("DERIVED_FROM", Entity, Event),
    ("OBSERVED", Entity, Event),
    ("MENTIONS", Event, Entity),
    ("DESCRIBES", Event, Entity),
    ("CAUSES", CausalVariable, CausalVariable),
    ("METRIC_INFLUENCE", Metric, Metric),
];

/// Checks an ordered relation signature without allocating.
pub fn validate_relation(
    label: &str,
    source: GraphNodeKind,
    target: GraphNodeKind,
) -> Result<()> {
    if !RELATION_ENDPOINTS.contains(&(label, source, target)) {
        bail!("Invalid structural graph relation: {label}");
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq)]
pub struct GraphEdge {
    pub from_id: String,
    pub to_id: String,
    pub label: String,
    pub properties: Value,
}

#[derive(Debug, Clone, PartialEq)]
pub struct GraphSubgraph {
    pub nodes: Vec<GraphNode>,
    pub edges: Vec<GraphEdge>,
}

#[async_trait::async_trait]
pub trait RelationsStore: Send + Sync {
    /// Retrieves a bounded k-hop neighborhood around `entity_id`.
    async fn k_hop_neighbors(
        &self,
        tenant_id: &str,
        graph_name: &str,
        entity_id: &str,
        k: u8,
        limit: usize,
    ) -> Result<GraphSubgraph>;
}
