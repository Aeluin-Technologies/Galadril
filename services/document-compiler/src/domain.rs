//! Queue state contains identifiers and delegation metadata, never document
//! bytes.
pub struct NewJob {
    pub id: String,
    pub tenant: String,
    pub actor: String,
    pub generation: String,
    pub fingerprint: String,
    pub capability: String,
    pub template_id: String,
    pub template_version: String,
}

#[derive(Clone)]
pub struct Job {
    pub id: String,
    pub tenant: String,
    pub actor: String,
    pub generation: String,
    pub fingerprint: String,
    pub capability: String,
    pub template_id: String,
    pub template_version: String,
    pub status: String,
    pub failure: String,
    pub lease: String,
    pub lease_until: i64,
    pub attempts: i32,
    pub created_at: i64,
}
