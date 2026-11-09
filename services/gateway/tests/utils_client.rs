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

#[path = "../src/adapters/inbound/mail_policy.rs"]
mod mail_policy;

#[test]
fn analysis_only_mailbox_source_does_not_grant_a_reply_policy() {
    let account = "a".repeat(32);
    let definition = serde_json::json!({"sources":[{"capability_id":"utils.input.email","account_id":account}],"pipeline":[{"capability_id":"utils.algorithm.email_context"}]});
    assert!(mail_policy::selects_account(&definition, &account));
    assert!(!mail_policy::selects_account(&definition, &"b".repeat(32)));
    assert!(mail_policy::policy(&definition, &account).is_none());
}

#[test]
fn deployed_scribe_filter_requires_a_durable_decision_before_sending() {
    let account = "a".repeat(32);
    let definition = serde_json::json!({"sources":[{"capability_id":"utils.input.email","account_id":account}],"pipeline":[{"capability_id":"utils.algorithm.decide_reply"},{"capability_id":"utils.output.reply","params":{"sender_account_id":"source","delivery_mode":"automatic"}}]});
    let policy = mail_policy::policy(&definition, &account);
    assert_eq!(
        policy.and_then(|value| value.get("decision_required").cloned()),
        Some(serde_json::Value::Bool(true))
    );
}

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
        "pipeline_reply",
        "pipeline_decide",
        "pipeline_context",
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
        "contacts",
        "messages",
        "message",
        "thread",
        "attachment",
        "drafts",
        "draft",
        "proposal_status",
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
