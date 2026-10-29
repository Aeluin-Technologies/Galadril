//! Deployed graph settings are the authority for mailbox execution.

use serde_json::{Value, json};

pub(super) fn policy(definition: &Value, account: &str) -> Option<Value> {
    let sources = definition.get("sources")?.as_array()?;
    if !sources.iter().any(|source| {
        source.get("capability_id").and_then(Value::as_str) ==
            Some("utils.input.email") &&
            source.get("account_id").and_then(Value::as_str) ==
                Some(account)
    }) {
        return None;
    }
    let mut outputs =
        definition
            .get("pipeline")?
            .as_array()?
            .iter()
            .filter(|step| {
                step.get("capability_id").and_then(Value::as_str) ==
                    Some("utils.output.reply")
            });
    let output = outputs.next()?;
    if outputs.next().is_some() {
        return None;
    }
    let parameters = output.get("params")?;
    let sender = parameters
        .get("sender_account_id")
        .and_then(Value::as_str)?;
    let sender = if sender == "source" { account } else { sender };
    if sender.len() != 32 ||
        !sender.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        return None;
    }
    let mode = parameters.get("delivery_mode").and_then(Value::as_str)?;
    if !matches!(mode, "automatic" | "approval" | "draft") {
        return None;
    }
    Some(json!({"sender_account_id": sender, "mode": mode}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deployed_mailbox_selection_is_explicit() {
        let account = "a".repeat(32);
        let definition = json!({"sources":[{"capability_id":"utils.input.email","account_id":account}],"pipeline":[{"capability_id":"utils.output.reply","params":{"sender_account_id":"source","delivery_mode":"automatic"}}]});
        assert_eq!(
            policy(&definition, &account),
            Some(json!({"sender_account_id":account,"mode":"automatic"}))
        );
        assert!(policy(&definition, &"b".repeat(32)).is_none());
        assert!(
            policy(&json!({"sources":[],"pipeline":[]}), &account).is_none()
        );
    }
}
