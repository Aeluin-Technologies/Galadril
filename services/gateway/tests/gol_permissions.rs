//! The production SpiceDB adapter must preserve freshness and caveat failures.

#[path = "../src/application/usecases/relationship_retry.rs"]
mod relationship_retry;
#[path = "../src/application/usecases/gol/spicedb.rs"]
mod spicedb;

use galadril_ontology::authorization::{Consistency, PermissionOutcome};
use loth::spicedb::pb::authzed::api::v1::check_permission_response::Permissionship;
use loth::spicedb::pb::authzed::api::v1::consistency::Requirement;
use loth::spicedb::pb::authzed::api::v1::{
    CheckPermissionRequest, CheckPermissionResponse,
    Consistency as WireConsistency, ObjectReference, ZedToken,
};
use loth::types::AuthError;

#[tokio::test]
async fn internal_spicedb_failure_is_retried_without_reusing_permissions() {
    let attempts = std::cell::Cell::new(0);
    let response = relationship_retry::resilient_structural_check(|| {
        attempts.set(attempts.get() + 1);
        std::future::ready(if attempts.get() == 1 {
            Err(AuthError::SpiceDbStatus {
                operation: "check_permission",
                status: tonic::Status::internal("private dependency detail"),
            })
        } else {
            Ok(CheckPermissionResponse {
                permissionship: Permissionship::NoPermission as i32,
                ..Default::default()
            })
        })
    })
    .await
    .map(|response| spicedb::outcome(response, &Consistency::FullyConsistent));
    assert!(matches!(response, Ok(PermissionOutcome::Deny)));
    assert_eq!(attempts.get(), 2);
}

#[test]
fn unresolved_and_unknown_permissions_never_become_allow() {
    let consistency = Consistency::FullyConsistent;
    for state in [
        Permissionship::ConditionalPermission as i32,
        Permissionship::NoPermission as i32,
        Permissionship::Unspecified as i32,
        999,
    ] {
        assert!(!matches!(
            spicedb::outcome(
                CheckPermissionResponse {
                    permissionship: state,
                    ..Default::default()
                },
                &consistency
            ),
            PermissionOutcome::Allow(_)
        ));
    }
    assert!(!matches!(
        spicedb::outcome(
            CheckPermissionResponse {
                permissionship: Permissionship::HasPermission as i32,
                ..Default::default()
            },
            &consistency
        ),
        PermissionOutcome::Allow(_)
    ));
    assert_eq!(
        spicedb::outcome(
            CheckPermissionResponse {
                permissionship: Permissionship::HasPermission as i32,
                checked_at: Some(ZedToken {
                    token: "current_revision".into()
                }),
                ..Default::default()
            },
            &consistency
        ),
        PermissionOutcome::Allow(consistency)
    );
}

#[tokio::test]
async fn read_scope_reuses_exact_checks_and_observes_next_scope_revocation()
-> Result<(), &'static str> {
    fn request(id: &str, revision: &str) -> CheckPermissionRequest {
        CheckPermissionRequest {
            resource: Some(ObjectReference {
                object_type: "record".into(),
                object_id: id.into(),
            }),
            consistency: Some(WireConsistency {
                requirement: Some(Requirement::AtExactSnapshot(ZedToken {
                    token: revision.into(),
                })),
            }),
            permission: "read".into(),
            ..Default::default()
        }
    }

    fn response(
        permission: Permissionship,
        revision: &str,
    ) -> CheckPermissionResponse {
        CheckPermissionResponse {
            permissionship: permission as i32,
            checked_at: Some(ZedToken {
                token: revision.into(),
            }),
            ..Default::default()
        }
    }

    let scope = spicedb::ReadSnapshot::default();
    let required = spicedb::snapshot_consistency(response(
        Permissionship::NoPermission,
        "revision",
    ));
    let Some(required) = required else {
        return Err("Missing fresh revision");
    };
    let seen = std::cell::RefCell::new(Vec::new());
    let transport = |rpc: CheckPermissionRequest| {
        seen.borrow_mut()
            .push(rpc.consistency.and_then(|wire| wire.requirement));
        std::future::ready(Some(response(
            Permissionship::HasPermission,
            "revision",
        )))
    };
    for id in ["one", "one", "two", "two"] {
        assert_eq!(
            scope
                .check(request(id, "revision"), &required, transport)
                .await,
            PermissionOutcome::Allow(required.clone())
        );
    }
    assert_eq!(
        *seen.borrow(),
        [
            Some(Requirement::AtExactSnapshot(ZedToken {
                token: "revision".into()
            })),
            Some(Requirement::AtExactSnapshot(ZedToken {
                token: "revision".into()
            })),
        ]
    );
    assert_eq!(
        scope
            .check(request("three", "revision"), &required, |_| {
                std::future::ready(Some(response(
                    Permissionship::HasPermission,
                    "unexpected",
                )))
            })
            .await,
        PermissionOutcome::Unavailable
    );
    let newer = Consistency::AtExactSnapshot("after-revocation".into());
    assert_eq!(
        spicedb::ReadSnapshot::default()
            .check(request("one", "after-revocation"), &newer, |_| {
                std::future::ready(Some(response(
                    Permissionship::NoPermission,
                    "after-revocation",
                )))
            })
            .await,
        PermissionOutcome::Deny
    );
    Ok(())
}
