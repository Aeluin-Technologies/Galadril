//! Outbound port for retrieving graph relations for an entity.

use anyhow::{Result, bail, ensure};
use galadril_ontology::archive::remote::Generation;
use galadril_ontology::{Name, OntologyId, Version};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct GraphNode {
    pub id: String,
    pub kind: GraphNodeKind,
    pub ontology_ref: Option<GolReference>,
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
            _ => bail!("Unsupported structural graph kind"),
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
            Self::Entity | Self::Event | Self::LatentIdentity => {
                Some("entity")
            },
            Self::State | Self::CausalVariable => Some("field"),
            Self::Inference | Self::Decision | Self::Metric => None,
        }
    }
}

/// Immutable compiled provenance; source revisions never stand in for
/// generations.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolReference {
    pub tenant_id: String,
    pub ontology_id: String,
    pub ontology_version: String,
    pub source_revision: String,
    pub generation_id: String,
    pub symbol_key: String,
    pub symbol_kind: String,
}

impl GolReference {
    /// Validates lexical provenance without resolving symbols or granting
    /// access.
    ///
    /// Structural nodes refer either to a GOL entity or to one explicit field.
    /// Generic error text prevents malformed private provenance from becoming
    /// an error-message disclosure before the runtime applies metadata
    /// authorization.
    pub fn validate(
        &self,
        tenant_id: &str,
        kind: GraphNodeKind,
    ) -> Result<()> {
        const INVALID: &str = "Invalid compiled GOL reference for graph node";
        ensure!(
            self.tenant_id == tenant_id &&
                !tenant_id.is_empty() &&
                tenant_id.len() <= 128 &&
                self.ontology_id.len() <= 128 &&
                self.ontology_version.len() <= 128 &&
                self.symbol_key.len() <= 512 &&
                galadril_registry::storage::valid_revision(
                    &self.source_revision
                ) &&
                kind.ontology_kind() == Some(self.symbol_kind.as_str()),
            INVALID
        );
        ensure!(self.ontology_id.parse::<OntologyId>().is_ok(), INVALID);
        ensure!(self.ontology_version.parse::<Version>().is_ok(), INVALID);
        ensure!(self.generation_id.parse::<Generation>().is_ok(), INVALID);
        let (module, local) = self
            .symbol_key
            .split_once("::")
            .ok_or_else(|| anyhow::anyhow!(INVALID))?;
        ensure!(module == self.ontology_id, INVALID);
        let (symbol, field) = match local.split_once('.') {
            Some((symbol, field)) => (symbol, Some(field)),
            None => (local, None),
        };
        ensure!(symbol.parse::<Name>().is_ok(), INVALID);
        match (self.symbol_kind.as_str(), field) {
            ("entity", None) => Ok(()),
            ("field", Some(name)) if name.parse::<Name>().is_ok() => Ok(()),
            _ => bail!(INVALID),
        }
    }
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
        bail!("Invalid structural graph relation");
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
    async fn relation_endpoints(
        &self,
        _tenant_id: &str,
        _graph_name: &str,
        _relation_id: &str,
    ) -> Result<(GraphNode, GraphNode)> {
        bail!("Relation context unavailable")
    }
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
