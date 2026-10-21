//! Display labels are independent of the identifiers used for authorization.

use anyhow::{Result, ensure};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DirectorySubject {
    User,
    Role,
}

impl DirectorySubject {
    /// Keeps subject kinds closed while matching the authorization directory.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::User => "user",
            Self::Role => "role",
        }
    }
}

/// Borrows the normalized label so validation does not allocate a second copy.
pub fn validate_display_name(value: &str) -> Result<&str> {
    let value = value.trim();
    ensure!(
        !value.is_empty()
            && value.len() <= 512
            && value.chars().count() <= 128
            && !value.chars().any(char::is_control),
        "Choose a display name with 1 to 128 characters and no control characters"
    );
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::validate_display_name;

    #[test]
    fn labels_preserve_unicode_and_trim_only_surrounding_space()
    -> anyhow::Result<()> {
        assert_eq!(
            validate_display_name("  Équipe opérations  ")?,
            "Équipe opérations"
        );
        assert_eq!(validate_display_name("Alex Martin")?, "Alex Martin");
        Ok(())
    }

    #[test]
    fn labels_reject_empty_control_and_unbounded_input() {
        for invalid in ["", "   ", "Alex\nMartin", "Alex\0Martin"] {
            assert!(validate_display_name(invalid).is_err());
        }
        assert!(validate_display_name(&"é".repeat(129)).is_err());
        assert!(validate_display_name(&"x".repeat(513)).is_err());
    }
}
