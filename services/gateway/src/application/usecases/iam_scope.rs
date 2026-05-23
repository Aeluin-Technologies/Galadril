//! Utilities for preventing privilege escalation when granting permissions.
//!
//! The central rule is: a caller can only grant permissions that are within
//! (subset of) their own effective permission envelope.

use serde_json::Value;

use crate::domain::permission::IamPermission;

pub fn can_grant_all(
    caller_effective: &[IamPermission],
    requested: &[IamPermission],
) -> bool {
    requested
        .iter()
        .all(|req| can_grant_one(caller_effective, req))
}

fn can_grant_one(
    caller_effective: &[IamPermission],
    req: &IamPermission,
) -> bool {
    caller_effective.iter().any(|allow| {
        allow.action == req.action &&
            allow.effect == req.effect &&
            scope_is_subset(&req.scope, &allow.scope)
    })
}

fn scope_is_subset(requested: &Value, allowed: &Value) -> bool {
    match (requested, allowed) {
        (Value::Null, Value::Null) => true,
        (Value::Bool(a), Value::Bool(b)) => a == b,
        (Value::Number(a), Value::Number(b)) => a == b,
        (Value::String(a), Value::String(b)) => a == b,

        (Value::Array(req), Value::Array(allow)) => {
            // Avoid "clever" semantics that can be exploited.
            req == allow
        },

        (Value::Object(req), Value::Object(allow)) => {
            req.iter().all(|(k, v_req)| {
                allow
                    .get(k)
                    .is_some_and(|v_allow| scope_is_subset(v_req, v_allow))
            })
        },

        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::domain::permission::Effect;

    #[test]
    fn scope_subset_objects() {
        assert!(scope_is_subset(&json!({"a": 1}), &json!({"a": 1, "b": 2})));
        assert!(!scope_is_subset(&json!({"a": 2}), &json!({"a": 1, "b": 2})));
        assert!(!scope_is_subset(&json!({"x": 1}), &json!({"a": 1})));
    }

    #[test]
    fn scope_subset_arrays_are_strict() {
        assert!(scope_is_subset(&json!([1, 2]), &json!([1, 2])));
        assert!(!scope_is_subset(&json!([1]), &json!([1, 2])));
    }

    #[test]
    fn can_grant_requires_matching_action_effect_and_scope_subset() {
        let caller = [IamPermission {
            effect: Effect::Allow,
            action: "Read".to_string(),
            scope: json!({"tenant_id": "t1"}),
        }];
        let req_ok = [IamPermission {
            effect: Effect::Allow,
            action: "Read".to_string(),
            scope: json!({"tenant_id": "t1"}),
        }];
        let req_bad = [IamPermission {
            effect: Effect::Allow,
            action: "Read".to_string(),
            scope: json!({"tenant_id": "t2"}),
        }];

        assert!(can_grant_all(&caller, &req_ok));
        assert!(!can_grant_all(&caller, &req_bad));
    }
}
