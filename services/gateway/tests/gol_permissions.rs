//! The production SpiceDB adapter must preserve freshness and caveat failures.

#[path = "../src/application/usecases/relationship_retry.rs"]
mod relationship_retry;
#[path = "../src/application/usecases/gol/spicedb.rs"]
mod spicedb;

use galadril_ontology::authorization::{Consistency, PermissionOutcome};
use loth::spicedb::pb::authzed::api::v1::check_permission_response::Permissionship;
use loth::spicedb::pb::authzed::api::v1::{CheckPermissionResponse, ZedToken};

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
