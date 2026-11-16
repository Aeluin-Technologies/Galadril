//! Monotone IFC state for one calculation, never for an authenticated session.

use std::collections::BTreeSet;
use std::fmt;

use super::flow::{Flow, rank};
use super::kernel;
use crate::{Classification, QualifiedName};

/// A subject's upper bound, independent of information already observed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Clearance {
    /// Maximum confidentiality classification admitted by the authority.
    pub classification: Classification,
    /// Every compartment the authenticated subject may observe.
    pub compartments: BTreeSet<QualifiedName>,
}

impl Clearance {
    /// Creates a ceiling without granting membership in any compartment.
    #[must_use]
    pub fn new(classification: Classification) -> Self {
        Self {
            classification,
            compartments: BTreeSet::new(),
        }
    }

    /// Requires both the classification and every information compartment.
    #[must_use]
    pub fn admits(&self, flow: &Flow) -> bool {
        kernel::write_level(
            rank(flow.classification),
            rank(self.classification),
        ) && flow.compartments.is_subset(&self.compartments)
    }
}

/// Bounded errors that do not disclose the labels or values involved.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FlowError {
    /// The calculation is executing another protected effect.
    Busy,
    /// A computation belongs to a different or unauthenticated principal.
    Binding,
    /// A read or input would exceed the subject's clearance.
    Clearance,
    /// The destination cannot retain all confidentiality obligations.
    WriteDown,
    /// A purpose or destination restriction forbids the actual release.
    Restriction,
    /// A failed observation makes subsequent control-dependent effects unsafe.
    Poisoned,
}

impl fmt::Display for FlowError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "IFC operation refused: {self:?}")
    }
}
impl std::error::Error for FlowError {}

/// Complete information dependencies of a calculation admitted by the host.
///
/// Construction performs label accounting, not isolation or authorization.
/// The trusted effect dispatcher must include input, control, cache and shared
/// state dependencies. A caller-supplied label is never proof of independence.
/// There is intentionally no reset, clearance increase or declassification.
#[derive(Debug)]
pub struct Computation {
    clearance: Clearance,
    current: Flow,
    state: kernel::State,
}

impl Computation {
    /// Includes all admitted inputs before any effect is permitted.
    ///
    /// The host must establish their provenance at a real execution boundary.
    /// Unknown dependencies must retain conservative restrictions.
    ///
    /// # Examples
    ///
    /// ```
    /// use galadril_ontology::Classification;
    /// use galadril_ontology::authorization::{Clearance, Computation, Flow};
    /// let mut computation = Computation::from_inputs(
    ///     Clearance::new(Classification::Restricted),
    ///     Flow::new(Classification::Public),
    /// )?;
    /// computation.check_write(
    ///     &Flow::new(Classification::Public),
    ///     &"Internal".parse()?,
    ///     &"Review".parse()?,
    /// )?;
    /// computation.observe(&Flow::new(Classification::Confidential))?;
    /// assert!(
    ///     computation
    ///         .check_write(
    ///             &Flow::new(Classification::Public),
    ///             &"Internal".parse()?,
    ///             &"Review".parse()?,
    ///         )
    ///         .is_err()
    /// );
    /// # Ok::<(), Box<dyn std::error::Error>>(())
    /// ```
    pub fn from_inputs(
        clearance: Clearance,
        inputs: Flow,
    ) -> Result<Self, FlowError> {
        if !clearance.admits(&inputs) {
            return Err(FlowError::Clearance);
        }
        Ok(Self {
            state: kernel::new_state(
                rank(inputs.classification),
                rank(clearance.classification),
            )
            .ok_or(FlowError::Clearance)?,
            clearance,
            current: inputs,
        })
    }

    /// Borrows live provenance; a failed observation cannot mint a lower
    /// snapshot for another calculation.
    pub fn current_label(&self) -> Result<&Flow, FlowError> {
        if kernel::is_poisoned_state(&self.state) {
            return Err(FlowError::Poisoned);
        }
        Ok(&self.current)
    }

    /// Borrows the authenticated upper bound; it is not a source label.
    #[must_use]
    pub fn max_clearance(&self) -> &Clearance {
        &self.clearance
    }

    /// Records data or a control dependency before its result is observed.
    pub fn observe(&mut self, data: &Flow) -> Result<(), FlowError> {
        if kernel::is_poisoned_state(&self.state) {
            return Err(FlowError::Poisoned);
        }
        if !kernel::observe_state(
            &mut self.state,
            rank(data.classification),
            self.clearance.admits(data),
        ) {
            return Err(FlowError::Clearance);
        }
        if !self.current.dominates(data) {
            self.current = self.current.join(data);
        }
        Ok(())
    }

    /// Makes failures sticky so their existence cannot steer a lower output.
    pub fn poison(&mut self) {
        kernel::poison_state(&mut self.state);
    }

    /// Requires the stored destination label to preserve the complete flow.
    pub fn check_write(
        &self,
        destination_label: &Flow,
        destination: &QualifiedName,
        purpose: &QualifiedName,
    ) -> Result<(), FlowError> {
        if kernel::is_poisoned_state(&self.state) {
            return Err(FlowError::Poisoned);
        }
        if !self.current.permits_release(destination, purpose) ||
            !destination_label.permits_release(destination, purpose)
        {
            return Err(FlowError::Restriction);
        }
        if !kernel::may_write_state(
            &self.state,
            rank(destination_label.classification),
            destination_label.dominates(&self.current),
        ) {
            return Err(FlowError::WriteDown);
        }
        Ok(())
    }
}
