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

#[path = "../src/adapters/inbound/resource_policy.rs"]
mod resource_policy;

fn resource_catalog() -> serde_json::Value {
    serde_json::json!({
        "fixture.input.watch": {"x-galadril-resource-binding": {
            "version": 1, "resource": "fixture_resource",
            "operation": {"kind": "select", "parameter": "selected"}
        }},
        "fixture.output.send": {"x-galadril-resource-binding": {
            "version": 1, "resource": "fixture_resource",
            "operation": {"kind": "execute", "parameter": "sender", "mode": "strategy"}
        }},
        "fixture.algorithm.review": {"x-galadril-resource-binding": {
            "version": 1, "resource": "fixture_resource",
            "operation": {"kind": "decide"}
        }}
    })
}

#[test]
fn an_arbitrary_registered_input_does_not_grant_an_external_action() {
    let catalog = resource_catalog();
    let lookup = |id: &str, version: Option<&str>| {
        (version == Some("1.2.0"))
            .then(|| catalog.get(id))
            .flatten()
    };
    let definition = serde_json::json!({
        "sources":[{"capability_id":"fixture.input.watch","capability_version":"1.2.0","selected":"resource-a"}],
        "pipeline":[{"type":"forward"}]
    });
    let grant = resource_policy::authorize(
        &definition,
        "fixture_resource",
        "resource-a",
        lookup,
    );
    assert!(matches!(grant, Some(grant) if grant.action.is_none()));
    assert!(
        resource_policy::authorize(
            &definition,
            "fixture_resource",
            "resource-b",
            lookup
        )
        .is_none()
    );
}

#[test]
fn an_unavailable_decision_cannot_silently_admit_an_external_action() {
    let catalog = resource_catalog();
    let lookup = |id: &str, version: Option<&str>| {
        (version == Some("1.2.0"))
            .then(|| catalog.get(id))
            .flatten()
    };
    let mut definition = serde_json::json!({
        "sources":[{"capability_id":"fixture.input.watch","capability_version":"1.2.0","selected":"resource-a"}],
        "pipeline":[
            {"capability_id":"fixture.algorithm.review","capability_version":"1.2.0"},
            {"capability_id":"fixture.output.send","capability_version":"1.2.0","params":{"sender":"source","strategy":"approval"}}
        ]
    });
    let action = resource_policy::authorize(
        &definition,
        "fixture_resource",
        "resource-a",
        lookup,
    )
    .and_then(|grant| grant.action);
    assert!(
        matches!(action, Some(action) if action.target == "resource-a"
        && action.decision_required && action.mode == resource_policy::ActionMode::Approval)
    );

    if let Some(version) =
        definition.pointer_mut("/pipeline/0/capability_version")
    {
        *version = serde_json::json!("2.0.0");
    }
    let grant = resource_policy::authorize(
        &definition,
        "fixture_resource",
        "resource-a",
        lookup,
    );
    assert!(matches!(grant, Some(grant) if grant.action.is_none()));
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
