//! Regression contracts for the private email bridge.

#[path = "../src/application/ports/email.rs"]
pub mod email_port;
mod application {
    pub mod ports {
        pub use crate::email_port as email;
    }
}

#[expect(
    dead_code,
    reason = "Standalone boundary tests cover policy without starting the production bridge"
)]
#[path = "../src/adapters/outbound/utils.rs"]
mod utils;

#[test]
fn delegated_models_cannot_manage_credentials_or_approve_their_own_drafts() {
    for operation in [
        "reveal",
        "create",
        "update",
        "delete",
        "grant",
        "approve",
        "send",
        "save_profile",
        "oauth_start",
    ] {
        assert!(
            utils::validate_tool_operation(
                &serde_json::json!({"operation":operation})
            )
            .is_err()
        );
    }
    for operation in [
        "accounts",
        "messages",
        "message",
        "drafts",
        "save_draft",
        "label",
    ] {
        assert!(
            utils::validate_tool_operation(
                &serde_json::json!({"operation":operation})
            )
            .is_ok()
        );
    }
}

#[test]
fn deployment_urls_cannot_carry_credentials_or_change_request_path() {
    for url in [
        "https://user:password@example.org",
        "https://example.org?token=secret",
        "file:///tmp/mail",
        "https://example.org/operation",
    ] {
        assert!(
            utils::UtilsClient::new(
                url,
                secrecy::SecretString::from("x".repeat(48))
            )
            .is_err()
        );
    }
    assert!(
        utils::UtilsClient::new(
            "http://127.0.0.1:8095",
            secrecy::SecretString::from("x".repeat(48))
        )
        .is_ok()
    );
}
