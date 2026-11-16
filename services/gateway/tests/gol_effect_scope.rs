//! An external effect retains its calculation boundary until completion.

#[path = "../src/application/usecases/gol/effect_scope.rs"]
mod effect_scope;

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use effect_scope::Permits;
use tokio::sync::Mutex;

type TestResult = Result<(), Box<dyn std::error::Error>>;

#[tokio::test]
async fn a_live_effect_excludes_other_calculation_inputs() -> TestResult {
    let permits = Permits::new(2, Duration::from_secs(2))?;
    let gate = Arc::new(Mutex::new(()));
    let invalid = Arc::new(AtomicBool::new(false));
    let guard = Arc::clone(&gate).lock_owned().await;
    let token = permits.begin(
        "tenant_a",
        "calculation_a",
        Arc::clone(&invalid),
        guard,
        Instant::now(),
    )?;
    assert!(gate.try_lock().is_err());
    assert!(!permits.complete(
        &token,
        "tenant_b",
        "calculation_a",
        true,
        Instant::now()
    )?);
    assert!(gate.try_lock().is_err());
    assert!(permits.complete(
        &token,
        "tenant_a",
        "calculation_a",
        true,
        Instant::now()
    )?);
    assert!(gate.try_lock().is_ok());
    assert!(!invalid.load(Ordering::Acquire));
    assert!(!permits.complete(
        &token,
        "tenant_a",
        "calculation_a",
        true,
        Instant::now()
    )?);
    Ok(())
}

#[tokio::test]
async fn failure_or_unknown_outcome_invalidates_before_unlock() -> TestResult {
    let now = Instant::now();
    for (success, elapsed) in
        [(false, Duration::ZERO), (true, Duration::from_secs(2))]
    {
        let permits = Permits::new(2, Duration::from_secs(1))?;
        let gate = Arc::new(Mutex::new(()));
        let invalid = Arc::new(AtomicBool::new(false));
        let token = permits.begin(
            "tenant_a",
            "calculation_a",
            Arc::clone(&invalid),
            Arc::clone(&gate).lock_owned().await,
            now,
        )?;
        let accepted = permits.complete(
            &token,
            "tenant_a",
            "calculation_a",
            success,
            now + elapsed,
        )?;
        assert_eq!(accepted, elapsed.is_zero());
        assert!(invalid.load(Ordering::Acquire));
        assert!(gate.try_lock().is_ok());
    }
    Ok(())
}

#[tokio::test]
async fn abandoned_effects_cannot_resume_a_lower_labeled_calculation()
-> TestResult {
    let now = Instant::now();
    let permits = Permits::new(1, Duration::from_secs(1))?;
    let gate = Arc::new(Mutex::new(()));
    let invalid = Arc::new(AtomicBool::new(false));
    let token = permits.begin(
        "tenant_a",
        "calculation_a",
        Arc::clone(&invalid),
        Arc::clone(&gate).lock_owned().await,
        now,
    )?;
    permits.reap(now + Duration::from_secs(1))?;
    assert!(invalid.load(Ordering::Acquire));
    assert!(gate.try_lock().is_ok());
    assert!(!permits.complete(
        &token,
        "tenant_a",
        "calculation_a",
        true,
        now + Duration::from_secs(1)
    )?);
    assert!(
        permits
            .begin(
                "tenant_a",
                "calculation_a",
                Arc::clone(&invalid),
                Arc::clone(&gate).lock_owned().await,
                now + Duration::from_secs(1)
            )
            .is_err()
    );
    Ok(())
}
