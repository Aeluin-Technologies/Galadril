//! Bind mutable IFC state to the verified actor of one admitted calculation.

use std::sync::Arc;

use tokio::sync::{Mutex, OwnedMutexGuard};

use super::{Clearance, Computation, Flow, FlowError, Principal};

/// The trusted dispatcher's computation state, shared by all dependent work.
///
/// Creating this object does not establish isolation. Dispatchers must carry
/// its state through dependent requests and include labeled shared state in
/// new admissions. Untrusted request IDs or task boundaries cannot reset it.
/// The actor binding prevents reuse of another principal's clearance.
pub struct ExecutionContext {
    issuer: Box<str>,
    tenant: Box<str>,
    subject_type: Box<str>,
    subject: Box<str>,
    state: Arc<Mutex<Computation>>,
}

impl ExecutionContext {
    /// Admits complete, host-verified input provenance for an authenticated
    /// actor.
    pub fn from_inputs(
        principal: &Principal,
        clearance: Clearance,
        inputs: Flow,
    ) -> Result<Self, FlowError> {
        if principal.issuer.is_empty() ||
            principal.tenant.is_empty() ||
            principal.subject_type.is_empty() ||
            principal.subject_id.is_empty()
        {
            return Err(FlowError::Binding);
        }
        Ok(Self {
            issuer: principal.issuer.clone(),
            tenant: principal.tenant.clone(),
            subject_type: principal.subject_type.clone(),
            subject: principal.subject_id.clone(),
            state: Arc::new(Mutex::new(Computation::from_inputs(
                clearance, inputs,
            )?)),
        })
    }

    /// Copies the protected label for an envelope or durable cache entry.
    pub fn current_label(&self) -> Result<Flow, FlowError> {
        let state = self.state.try_lock().map_err(|_| FlowError::Busy)?;
        Ok(state.current_label()?.clone())
    }

    /// Includes a value received from another context without lowering its
    /// label.
    pub fn include_input(&self, flow: &Flow) -> Result<(), FlowError> {
        self.state
            .try_lock()
            .map_err(|_| FlowError::Busy)?
            .observe(flow)
    }

    /// Forks dependent work, including captured values and control inputs.
    /// A task boundary cannot discard the parent's restrictions or failures.
    pub fn fork(&self, inputs: &Flow) -> Result<Arc<Self>, FlowError> {
        let mut parent = self.state.try_lock().map_err(|_| FlowError::Busy)?;
        parent.observe(inputs)?;
        let state = Computation::from_inputs(
            parent.max_clearance().clone(),
            parent.current_label()?.clone(),
        )?;
        Ok(Arc::new(Self {
            issuer: self.issuer.clone(),
            tenant: self.tenant.clone(),
            subject_type: self.subject_type.clone(),
            subject: self.subject.clone(),
            state: Arc::new(Mutex::new(state)),
        }))
    }

    pub(super) async fn lock(
        &self,
        principal: &Principal,
    ) -> Result<OwnedMutexGuard<Computation>, FlowError> {
        if self.issuer != principal.issuer ||
            self.tenant != principal.tenant ||
            self.subject_type != principal.subject_type ||
            self.subject != principal.subject_id
        {
            return Err(FlowError::Binding);
        }
        Ok(Arc::clone(&self.state).lock_owned().await)
    }
}
