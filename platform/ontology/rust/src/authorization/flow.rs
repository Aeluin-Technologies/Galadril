//! IFC confidentiality and conservative composition of release restrictions.

use std::collections::BTreeSet;

use super::kernel;
use crate::{Classification, QualifiedName};

/// The effective confidentiality and release requirements of a value.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Flow {
    /// Required confidentiality clearance.
    pub classification: Classification,
    /// All compartments a recipient must be cleared for.
    pub compartments: BTreeSet<QualifiedName>,
    /// An explicit destination restriction. An empty set denies all
    /// destinations.
    pub destinations: Option<BTreeSet<QualifiedName>>,
    /// An explicit purpose restriction. An empty set denies all purposes.
    pub purposes: Option<BTreeSet<QualifiedName>>,
}

impl Flow {
    /// Creates a classification with no additional label restrictions.
    #[must_use]
    pub fn new(classification: Classification) -> Self {
        Self {
            classification,
            compartments: BTreeSet::new(),
            destinations: None,
            purposes: None,
        }
    }

    /// Joins confidentiality and compartments, intersecting release lists.
    ///
    /// Absence of one list is the identity for that list's intersection. The
    /// decision gate still requires an approved sink and purpose.
    #[must_use]
    pub fn join(&self, other: &Self) -> Self {
        let classification = match kernel::join_level(
            rank(self.classification),
            rank(other.classification),
        ) {
            0 => Classification::Public,
            1 => Classification::Internal,
            2 => Classification::Confidential,
            _ => Classification::Restricted,
        };
        Self {
            classification,
            compartments: self
                .compartments
                .union(&other.compartments)
                .cloned()
                .collect(),
            destinations: restrict(&self.destinations, &other.destinations),
            purposes: restrict(&self.purposes, &other.purposes),
        }
    }

    /// Checks whether this flow is at least as restrictive in every dimension.
    #[must_use]
    pub fn dominates(&self, other: &Self) -> bool {
        kernel::write_level(
            rank(other.classification),
            rank(self.classification),
        ) && other.compartments.is_subset(&self.compartments) &&
            narrows(&self.destinations, &other.destinations) &&
            narrows(&self.purposes, &other.purposes)
    }

    pub(super) fn permits_release(
        &self,
        destination: &QualifiedName,
        purpose: &QualifiedName,
    ) -> bool {
        self.destinations
            .as_ref()
            .is_none_or(|set| set.contains(destination)) &&
            self.purposes
                .as_ref()
                .is_none_or(|set| set.contains(purpose))
    }
}

pub(super) fn rank(classification: Classification) -> u8 {
    match classification {
        Classification::Public => 0,
        Classification::Internal => 1,
        Classification::Confidential => 2,
        Classification::Restricted => 3,
    }
}

fn restrict(
    left: &Option<BTreeSet<QualifiedName>>,
    right: &Option<BTreeSet<QualifiedName>>,
) -> Option<BTreeSet<QualifiedName>> {
    match (left, right) {
        (Some(left), Some(right)) => {
            Some(left.intersection(right).cloned().collect())
        },
        (left, right) => left.as_ref().or(right.as_ref()).cloned(),
    }
}

fn narrows(
    left: &Option<BTreeSet<QualifiedName>>,
    right: &Option<BTreeSet<QualifiedName>>,
) -> bool {
    right.as_ref().is_none_or(|right| {
        left.as_ref().is_some_and(|left| left.is_subset(right))
    })
}

/// The boundary a registered sink resides in.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum SinkKind {
    /// A destination inside the deployment's trusted read boundary.
    Internal,
    /// A destination reached only by a transfer route.
    External,
}

/// A destination's clearance and authorized purposes, supplied by the host.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone)]
#[non_exhaustive]
pub struct Sink {
    /// Determines whether an explicit transfer route is mandatory.
    pub kind: SinkKind,
    /// Maximum classification this destination can receive.
    pub clearance: Classification,
    /// Compartments the destination can receive.
    pub compartments: BTreeSet<QualifiedName>,
    /// Purpose allowlist. An empty set admits no purposes.
    pub purposes: BTreeSet<QualifiedName>,
}

impl Sink {
    /// Starts a sink with no compartment or purpose approvals.
    #[must_use]
    pub fn new(kind: SinkKind, clearance: Classification) -> Self {
        Self {
            kind,
            clearance,
            compartments: BTreeSet::new(),
            purposes: BTreeSet::new(),
        }
    }

    pub(super) fn admits(
        &self,
        flow: &Flow,
        destination: &QualifiedName,
        purpose: &QualifiedName,
    ) -> bool {
        kernel::write_level(rank(flow.classification), rank(self.clearance)) &&
            flow.compartments.is_subset(&self.compartments) &&
            self.purposes.contains(purpose) &&
            flow.permits_release(destination, purpose)
    }
}
