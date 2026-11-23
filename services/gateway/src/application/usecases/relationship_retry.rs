//! Share the retry policy for idempotent SpiceDB permission checks.

use std::future::Future;
use std::time::Duration;

use loth::types::AuthError;

/// Retries only idempotent dependency failures; every retry reads current
/// policy.
pub(super) async fn resilient_structural_check<T, F, Fut>(
    mut check: F,
) -> Result<T, AuthError>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T, AuthError>>,
{
    for attempt in 0..3_u32 {
        match check().await {
            Err(AuthError::SpiceDbStatus { operation, status })
                if attempt < 2 &&
                    matches!(
                        status.code(),
                        tonic::Code::Cancelled |
                            tonic::Code::Unavailable |
                            tonic::Code::DeadlineExceeded |
                            tonic::Code::ResourceExhausted |
                            tonic::Code::Aborted
                    ) =>
            {
                tracing::warn!(event.name = "authorization.check.retry", attempt = attempt + 1, grpc_code = ?status.code(), operation);
                tokio::time::sleep(Duration::from_millis(
                    100 * u64::from(attempt + 1),
                ))
                .await;
            },
            result => return result,
        }
    }
    Err(AuthError::ValidationError {
        message: "Authorization retry budget exhausted".into(),
    })
}
