//! Bounded indexing keeps source buffers small and isolates document failures.

use std::future::Future;

use anyhow::Result;
use futures::StreamExt as _;

pub(crate) const BATCH_SIZE: usize = 2;

/// Polls every lease while keeping source buffers bounded and draining
/// failures independently.
pub(crate) async fn run<F: Future<Output = Result<()>>>(
    operations: impl IntoIterator<Item = F>,
) -> Result<()> {
    let pending =
        futures::stream::iter(operations).buffer_unordered(BATCH_SIZE);
    futures::pin_mut!(pending);
    let mut failure = None;
    while let Some(result) = pending.next().await {
        if let Err(error) = result &&
            failure.is_none()
        {
            failure = Some(error);
        }
    }
    match failure {
        Some(error) => Err(error),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[tokio::test]
    async fn indexing_overlaps_two_documents_without_starting_unbounded_work()
    -> anyhow::Result<()> {
        let active = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        let completed = Arc::new(AtomicUsize::new(0));
        let operations = (0..5).map(|_| {
            let active = Arc::clone(&active);
            let peak = Arc::clone(&peak);
            let completed = Arc::clone(&completed);
            async move {
                let count = active.fetch_add(1, Ordering::SeqCst) + 1;
                peak.fetch_max(count, Ordering::SeqCst);
                tokio::task::yield_now().await;
                active.fetch_sub(1, Ordering::SeqCst);
                completed.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
        });
        super::run(operations).await?;
        assert_eq!(peak.load(Ordering::SeqCst), 2);
        assert_eq!(completed.load(Ordering::SeqCst), 5);
        Ok(())
    }

    #[tokio::test]
    async fn one_failed_document_does_not_cancel_other_document_checkpoints() {
        let completed = AtomicUsize::new(0);
        let operations = (0..3).map(|index| {
            let completed = &completed;
            async move {
                tokio::task::yield_now().await;
                completed.fetch_add(1, Ordering::SeqCst);
                anyhow::ensure!(index != 0, "source unavailable");
                Ok(())
            }
        });
        assert!(super::run(operations).await.is_err());
        assert_eq!(completed.load(Ordering::SeqCst), 3);
    }
}
