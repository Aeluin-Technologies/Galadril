//! Retain the calculation boundary while a trusted worker performs I/O.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use tokio::sync::OwnedMutexGuard;
use uuid::Uuid;

struct Permit {
    tenant: Box<str>,
    calculation: Box<str>,
    invalid: Arc<AtomicBool>,
    expires: Instant,
    completed: bool,
    _boundary: OwnedMutexGuard<()>,
}

impl Drop for Permit {
    fn drop(&mut self) {
        // Unknown outcomes must invalidate before the exclusion guard drops.
        if !self.completed {
            self.invalid.store(true, Ordering::Release);
        }
    }
}

/// A lost response or worker never turns an uncertain calculation into a new
/// one. The registry bounds retained guards independently of worker traffic.
pub(super) struct Permits {
    entries: Mutex<HashMap<Uuid, Permit>>,
    capacity: usize,
    lifetime: Duration,
}

impl Permits {
    pub fn new(capacity: usize, lifetime: Duration) -> Result<Self> {
        ensure!(
            (1..=1024).contains(&capacity) &&
                lifetime > Duration::ZERO &&
                lifetime <= Duration::from_secs(120),
            "Invalid GOL effect bounds"
        );
        Ok(Self {
            entries: Mutex::new(HashMap::with_capacity(capacity)),
            capacity,
            lifetime,
        })
    }

    /// Ownership of the guard prevents another input from racing the actual
    /// I/O.
    pub fn begin(
        &self,
        tenant: &str,
        calculation: &str,
        invalid: Arc<AtomicBool>,
        boundary: OwnedMutexGuard<()>,
        now: Instant,
    ) -> Result<String> {
        ensure!(
            !tenant.is_empty() &&
                tenant.len() <= 128 &&
                !calculation.is_empty() &&
                calculation.len() <= 128 &&
                !invalid.load(Ordering::Acquire),
            "GOL effect unavailable"
        );
        let mut entries = self
            .entries
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL effects unavailable"))?;
        entries.retain(|_, permit| now < permit.expires);
        ensure!(entries.len() < self.capacity, "GOL effect capacity reached");
        let id = Uuid::new_v4();
        ensure!(!entries.contains_key(&id), "GOL effect unavailable");
        entries.insert(
            id,
            Permit {
                tenant: tenant.into(),
                calculation: calculation.into(),
                invalid,
                expires: now
                    .checked_add(self.lifetime)
                    .context("GOL effect lifetime unavailable")?,
                completed: false,
                _boundary: boundary,
            },
        );
        Ok(id.simple().to_string())
    }

    /// Only the authenticated scope can complete its own pending effect.
    pub fn complete(
        &self,
        token: &str,
        tenant: &str,
        calculation: &str,
        succeeded: bool,
        now: Instant,
    ) -> Result<bool> {
        if token.len() != 32 ||
            !token.bytes().all(|value| value.is_ascii_hexdigit())
        {
            return Ok(false);
        }
        let Ok(id) = Uuid::parse_str(token) else {
            return Ok(false);
        };
        let mut entries = self
            .entries
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL effects unavailable"))?;
        let Some(permit) = entries.get(&id) else {
            return Ok(false);
        };
        if permit.tenant.as_ref() != tenant ||
            permit.calculation.as_ref() != calculation
        {
            return Ok(false);
        }
        let Some(mut permit) = entries.remove(&id) else {
            return Ok(false);
        };
        let live = now < permit.expires;
        if succeeded && live {
            // Disarm the drop guard only after recording a known successful
            // outcome.
            permit.completed = true;
        }
        drop(permit);
        Ok(live)
    }

    /// Expiration poisons retained state before allowing queued requests to
    /// run.
    pub fn reap(&self, now: Instant) -> Result<()> {
        self.entries
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL effects unavailable"))?
            .retain(|_, permit| now < permit.expires);
        Ok(())
    }
}
