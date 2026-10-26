//! Durable queue transitions fence concurrent workers and survive restarts.
#[path = "../src/domain.rs"]
pub mod domain;
#[path = "../src/ports.rs"]
pub mod ports;
#[path = "../src/adapters/queue.rs"]
pub mod queue;
use domain::NewJob;
use queue::Queue;

fn job(id: char, tenant: &str) -> NewJob {
    NewJob {
        id: id.to_string().repeat(64),
        tenant: tenant.into(),
        actor: "actor".into(),
        generation: "generation".into(),
        fingerprint: id.to_string().repeat(64),
        capability: "a".repeat(64),
        template_id: "c".repeat(32),
        template_version: "d".repeat(64),
    }
}

#[test]
fn leases_are_atomic_and_stale_workers_cannot_publish() -> anyhow::Result<()> {
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("queue.db");
    let mut first = Queue::open(&path)?;
    let mut second = Queue::open(&path)?;
    first.enqueue(&job('a', "tenant"), 0)?;
    let lease = first
        .claim(0)?
        .ok_or_else(|| anyhow::anyhow!("Missing work"))?;
    assert!(second.claim(0)?.is_none());
    let replacement = second
        .claim(100_000)?
        .ok_or_else(|| anyhow::anyhow!("Missing recovered work"))?;
    assert!(!first.finish(&lease, "completed", "")?);
    assert!(second.finish(&replacement, "completed", "")?);
    assert_eq!(first.get("tenant", &lease.id)?.status, "completed");
    Ok(())
}

#[test]
fn restart_idempotence_tenant_isolation_and_cancellation_are_durable()
-> anyhow::Result<()> {
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("queue.db");
    let item = job('b', "tenant");
    let mut queue = Queue::open(&path)?;
    queue.enqueue(&item, 0)?;
    queue.enqueue(&item, 0)?;
    let mut conflict = job('b', "tenant");
    conflict.fingerprint = "c".repeat(64);
    assert!(queue.enqueue(&conflict, 0).is_err());
    assert!(queue.get("other", &item.id).is_err());
    let lease = queue
        .claim(0)?
        .ok_or_else(|| anyhow::anyhow!("Missing work"))?;
    drop(queue);
    let mut recovered = Queue::open(&path)?;
    recovered.cancel("tenant", &item.id)?;
    assert!(!recovered.finish(&lease, "completed", "")?);
    assert_eq!(recovered.get("tenant", &item.id)?.status, "cancelled");
    Ok(())
}

#[test]
fn competing_controllers_claim_one_job_without_lock_errors()
-> anyhow::Result<()> {
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("queue.db");
    Queue::open(&path)?.enqueue(&job('c', "tenant"), 0)?;
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let mut threads = Vec::new();
    for _ in 0..2 {
        let mut queue = Queue::open(&path)?;
        let barrier = std::sync::Arc::clone(&barrier);
        threads.push(std::thread::spawn(move || {
            barrier.wait();
            queue.claim(0)
        }));
    }
    let mut claimed = 0;
    for thread in threads {
        if thread
            .join()
            .map_err(|_| anyhow::anyhow!("Queue thread panicked"))??
            .is_some()
        {
            claimed += 1;
        }
    }
    assert_eq!(claimed, 1);
    Ok(())
}

#[test]
fn artifact_retention_is_seven_days_and_does_not_delete_running_jobs()
-> anyhow::Result<()> {
    let directory = tempfile::tempdir()?;
    let mut queue = Queue::open(directory.path().join("queue.db"))?;
    queue.enqueue(&job('d', "tenant"), 0)?;
    let completed = queue
        .claim(0)?
        .ok_or_else(|| anyhow::anyhow!("Missing job"))?;
    queue.finish(&completed, "completed", "")?;
    queue.enqueue(&job('e', "tenant"), 0)?;
    let running = queue
        .claim(0)?
        .ok_or_else(|| anyhow::anyhow!("Missing running job"))?;
    let deadline = 7 * 24 * 60 * 60 * 1000;
    assert!(queue.expired(deadline - 1)?.is_empty());
    let expired = queue.expired(deadline)?;
    assert_eq!(expired.len(), 1);
    assert_eq!(
        expired.first().map(|job| job.id.as_str()),
        Some(completed.id.as_str())
    );
    queue.forget(&completed)?;
    assert!(queue.get("tenant", &completed.id).is_err());
    queue.forget(&running)?;
    assert_eq!(queue.get("tenant", &running.id)?.status, "running");
    Ok(())
}
