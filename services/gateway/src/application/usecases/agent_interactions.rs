//! Human answers require both current resource access and generation
//! ownership.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuestionError {
    Forbidden,
    Invalid,
    Rejected,
    Unavailable,
}
impl std::fmt::Display for QuestionError {
    fn fmt(
        &self,
        formatter: &mut std::fmt::Formatter<'_>,
    ) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Forbidden => "Question access denied",
            Self::Invalid => "Invalid question response",
            Self::Rejected => "Question is no longer awaiting this response",
            Self::Unavailable => "Question response unavailable",
        })
    }
}
impl std::error::Error for QuestionError {}

pub fn answer_authority(
    owner: &str,
    user: &str,
    status: &str,
) -> Result<(), QuestionError> {
    if owner != user {
        return Err(QuestionError::Forbidden);
    }
    if !matches!(status, "pending" | "running") {
        return Err(QuestionError::Rejected);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_initiator_can_answer_an_active_generation() {
        for status in ["pending", "running"] {
            assert_eq!(answer_authority("owner", "owner", status), Ok(()));
            assert_eq!(
                answer_authority("owner", "reader", status),
                Err(QuestionError::Forbidden)
            );
        }
        for status in ["completed", "failed", "cancelled", "unknown"] {
            assert_eq!(
                answer_authority("owner", "owner", status),
                Err(QuestionError::Rejected)
            );
            assert_eq!(
                answer_authority("owner", "reader", status),
                Err(QuestionError::Forbidden)
            );
        }
    }
}
