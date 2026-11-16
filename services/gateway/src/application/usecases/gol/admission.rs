//! Keep actor-bound IFC provenance through dependent worker requests.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use cedar_policy::EntityUid;
use galadril_ontology::authorization::{
    Clearance, ExecutionContext, Flow, Principal,
};
use uuid::Uuid;

/// Verified identity and independently provisioned IAM ceiling.
pub(super) struct Actor {
    pub issuer: Box<str>,
    pub tenant: Box<str>,
    pub subject_type: Box<str>,
    pub subject_id: Box<str>,
    pub cedar: EntityUid,
    pub clearance: Clearance,
}

impl Actor {
    /// Preserves the IAM binding when assembling owned GOL evidence.
    pub fn principal(&self) -> Principal {
        Principal {
            issuer: self.issuer.clone(),
            tenant: self.tenant.clone(),
            subject_type: self.subject_type.clone(),
            subject_id: self.subject_id.clone(),
            cedar: self.cedar.clone(),
        }
    }
}

/// Provenance established by trusted ingress, never by the command payload.
pub(super) struct VerifiedInput {
    pub source_object_id: Box<str>,
    pub revision: Box<str>,
    pub flow: Flow,
}

pub(super) struct Admission {
    pub actor: Actor,
    pub input: VerifiedInput,
    pub context: Arc<ExecutionContext>,
    expires: Instant,
}

/// Bounded capabilities expire closed; eviction cannot create a fresh context.
pub(super) struct AdmissionRegistry {
    entries: Mutex<HashMap<Uuid, Arc<Admission>>>,
    capacity: usize,
    lifetime: Duration,
}

impl AdmissionRegistry {
    /// Limits retained security state independently of incoming worker
    /// traffic.
    pub fn new(capacity: usize, lifetime: Duration) -> Result<Self> {
        ensure!(
            (1..=4096).contains(&capacity) &&
                (Duration::from_secs(1)..=Duration::from_secs(900))
                    .contains(&lifetime),
            "Invalid GOL admission bounds"
        );
        Ok(Self {
            entries: Mutex::new(HashMap::with_capacity(capacity)),
            capacity,
            lifetime,
        })
    }

    /// Admits one input whose ownership, immutable revision and floor were
    /// verified by the authenticated dispatcher. A new request alone is not
    /// sufficient provenance to call this method.
    pub fn admit(
        &self,
        actor: Actor,
        input: VerifiedInput,
        now: Instant,
    ) -> Result<String> {
        ensure!(
            input.source_object_id.len() <= 1024 &&
                input
                    .source_object_id
                    .starts_with(&format!("raw:{}/", actor.tenant)) &&
                !input.revision.is_empty() &&
                input.revision.len() <= 1024,
            "Invalid GOL input provenance"
        );
        let context = Arc::new(ExecutionContext::from_inputs(
            &actor.principal(),
            actor.clearance.clone(),
            input.flow.clone(),
        )?);
        let mut entries = self
            .entries
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL admissions unavailable"))?;
        entries.retain(|_, entry| now < entry.expires);
        ensure!(
            entries.len() < self.capacity,
            "GOL admission capacity reached"
        );
        let id = Uuid::new_v4();
        // A collision must fail rather than replace another calculation.
        ensure!(!entries.contains_key(&id), "GOL admission unavailable");
        entries.insert(
            id,
            Arc::new(Admission {
                actor,
                input,
                context,
                expires: now
                    .checked_add(self.lifetime)
                    .context("Invalid GOL admission lifetime")?,
            }),
        );
        tracing::debug!(event.name = "gol.admission.created");
        Ok(id.simple().to_string())
    }

    /// Resolves only an unexpired handle for its original tenant and source.
    /// Every dependent request obtains the same monotone computation state.
    pub fn resolve(
        &self,
        token: &str,
        tenant: &str,
        source_object_id: &str,
        now: Instant,
    ) -> Result<Option<Arc<Admission>>> {
        if token.len() != 32 ||
            !token.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Ok(None);
        }
        let Ok(id) = Uuid::parse_str(token) else {
            return Ok(None);
        };
        let entries = self
            .entries
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL admissions unavailable"))?;
        Ok(entries
            .get(&id)
            .filter(|entry| {
                now < entry.expires &&
                    entry.actor.tenant.as_ref() == tenant &&
                    entry.input.source_object_id.as_ref() ==
                        source_object_id
            })
            .map(Arc::clone))
    }
}
