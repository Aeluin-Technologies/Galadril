//! MCP scopes attenuate existing IAM; they never authorize draft approval.

#[path = "../src/domain/mcp_workspace.rs"]
#[expect(dead_code, reason = "Standalone policy tests omit service wiring")]
mod policy;

use anyhow::Result;
use serde_json::json;

#[test]
fn oauth_mail_scopes_cannot_approve_manage_credentials_or_forge_identity()
-> Result<()> {
    assert_eq!(
        policy::required_scope(
            &json!({"operation":"email","request":{"operation":"messages"}})
        )?,
        "mail:read"
    );
    assert_eq!(
        policy::required_scope(
            &json!({"operation":"email","request":{"operation":"save_draft"}})
        )?,
        "mail:draft"
    );
    for operation in ["approve", "send", "reveal", "grant", "save_profile"] {
        assert!(
            policy::required_scope(
                &json!({"operation":"email","request":{"operation":operation}})
            )
            .is_err()
        );
    }
    for operation in [
        "mcp_delegate",
        "mcp_authorize",
        "mcp_invoke",
        "agent_state",
        "conversation_memory",
    ] {
        assert!(
            policy::required_scope(&json!({"operation":operation})).is_err()
        );
    }
    assert!(
        serde_json::from_value::<policy::WorkspaceRequest>(
            json!({"tenant":"other", "request":{"operation":"search"}})
        )
        .is_err()
    );
    Ok(())
}

#[test]
fn document_read_scope_does_not_grant_mutations() -> Result<()> {
    assert_eq!(
        policy::required_scope(
            &json!({"operation":"document_templates","request":{"action":"list"}})
        )?,
        "documents:read"
    );
    assert_eq!(
        policy::required_scope(
            &json!({"operation":"document_templates","request":{"action":"put"}})
        )?,
        "documents:write"
    );
    assert_eq!(
        policy::required_scope(&json!({"operation":"compile_document"}))?,
        "documents:write"
    );
    assert!(policy::required_scope(&json!({"operation":"document_templates","request":{"action":"enable_plugin"}})).is_err());
    Ok(())
}

#[test]
fn draft_results_never_return_compiled_pdf_bytes_to_the_model() {
    let result = policy::draft_projection(
        json!({"id":"draft", "revision":1, "content":{"body":"Public draft", "attachments":[{"name":"letter.pdf", "content_type":"application/pdf", "content":"PRIVATE_COMPILED_PDF"}]}}),
    );
    assert_eq!(
        result.pointer("/content/body"),
        Some(&json!("Public draft"))
    );
    assert_eq!(
        result.pointer("/content/attachments/0/name"),
        Some(&json!("letter.pdf"))
    );
    assert!(result.pointer("/content/attachments/0/content").is_none());
    assert!(!result.to_string().contains("PRIVATE_COMPILED_PDF"));
}

#[test]
fn execution_scope_is_stable_but_separates_users_tenants_clients_and_projects()
{
    let scope = policy::execution_id("tenant", "user", "client", None);
    assert_eq!(
        scope,
        policy::execution_id("tenant", "user", "client", None)
    );
    for other in [
        policy::execution_id("other", "user", "client", None),
        policy::execution_id("tenant", "other", "client", None),
        policy::execution_id("tenant", "user", "other", None),
        policy::execution_id("tenant", "user", "client", Some("project")),
    ] {
        assert_ne!(scope, other);
    }
}

#[test]
fn native_mcp_cannot_escape_a_specialized_context_or_enable_the_library() {
    assert!(policy::generation_operation_allowed(
        "compile_document",
        true,
        true
    ));
    assert!(!policy::generation_operation_allowed("email", true, true));
    assert!(!policy::generation_operation_allowed("search", true, true));
    assert!(!policy::generation_operation_allowed(
        "document_templates",
        false,
        false
    ));
    assert!(policy::generation_operation_allowed("email", false, false));
    for operation in [
        "mcp_delegate",
        "mcp_authorize",
        "agent_state",
        "conversation_memory",
        "unknown",
    ] {
        assert!(!policy::generation_operation_allowed(
            operation, false, true
        ));
    }
}
