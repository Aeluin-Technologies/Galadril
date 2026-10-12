//! Bounded, human-readable general access restrictions compiled to Cedar.

use anyhow::{Result, ensure};
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct GeneralAccessConfig {
    #[serde(default)]
    pub role_names: Vec<String>,
    pub start_hour_utc: Option<u8>,
    pub end_hour_utc: Option<u8>,
    #[serde(default)]
    pub regions: Vec<String>,
}

/// Compiles restrictions as forbids so other permit policies cannot weaken
/// them.
pub fn build_general_access_policy(
    config: &GeneralAccessConfig,
) -> Result<String> {
    ensure!(config.role_names.len() <= 64, "Choose at most 64 roles");
    for role in &config.role_names {
        ensure!(
            !role.is_empty() &&
                role.len() <= 64 &&
                role.bytes().all(|byte| byte.is_ascii_alphanumeric() ||
                    matches!(byte, b'_' | b'-')),
            "Choose a valid role from this organization"
        );
    }
    ensure!(
        config.regions.len() <= 32,
        "Choose at most 32 trusted regions"
    );
    for region in &config.regions {
        ensure!(
            !region.is_empty() &&
                region.len() <= 64 &&
                region.bytes().all(|byte| byte.is_ascii_alphanumeric() ||
                    matches!(byte, b' ' | b'_' | b'-')),
            "Choose a valid trusted region"
        );
    }
    let hours = match (config.start_hour_utc, config.end_hour_utc) {
        (None, None) => None,
        (Some(start), Some(end)) => {
            ensure!(
                start < 24 && end <= 24 && end > 0 && start != end,
                "Choose different start and end hours between 00:00 and 24:00 UTC"
            );
            let separator = if start < end { "&&" } else { "||" };
            Some(format!(
                "(context.hour_utc >= {start} {separator} context.hour_utc < {end})"
            ))
        },
        _ => anyhow::bail!("Choose both a start and an end hour in UTC"),
    };
    ensure!(
        hours.is_some() || !config.regions.is_empty(),
        "Choose an access time or a trusted region before saving"
    );
    let selected = if config.role_names.is_empty() {
        "true".to_owned()
    } else {
        format!(
            "context.roles.containsAny({})",
            serde_json::to_string(&config.role_names)?
        )
    };
    let mut conditions = Vec::with_capacity(2);
    if let Some(hours) = hours {
        conditions.push(format!("!{hours}"));
    }
    if !config.regions.is_empty() {
        conditions.push(format!(
            "!{}.contains(context.region)",
            serde_json::to_string(&config.regions)?
        ));
    }
    Ok(format!(
        "permit(principal, action, resource) when {{ context.is_structural_allowed }};\nforbid(principal, action, resource) when {{ {selected} && ({}) }};",
        conditions.join(" || ")
    ))
}

#[cfg(test)]
mod tests {
    use anyhow::Result;
    use cedar_policy::{
        Authorizer, Context, Decision, Entities, PolicySet, Request,
    };

    use super::*;

    fn allowed(
        policy: &str,
        roles: &[&str],
        hour: i64,
        region: &str,
    ) -> Result<bool> {
        let policies = policy.parse::<PolicySet>()?;
        let context = Context::from_json_value(
            serde_json::json!({
                "is_structural_allowed": true,
                "roles": roles,
                "hour_utc": hour,
                "region": region,
            }),
            None,
        )?;
        let request = Request::new(
            "User::\"alice\"".parse()?,
            "Action::\"view\"".parse()?,
            "document::\"acme/report\"".parse()?,
            context,
            None,
        )?;
        Ok(Authorizer::new()
            .is_authorized(&request, &policies, &Entities::empty())
            .decision() ==
            Decision::Allow)
    }

    #[test]
    fn generated_restrictions_target_selected_roles_and_exact_hours()
    -> Result<()> {
        let config = serde_json::from_value(serde_json::json!({
            "roleNames": ["analyst"], "startHourUtc": 8, "endHourUtc": 18,
            "regions": ["EU"]
        }))?;
        let policy = build_general_access_policy(&config)?;
        for (roles, hour, region, expected) in [
            (vec!["analyst"], 8, "EU", true),
            (vec!["analyst"], 17, "EU", true),
            (vec!["analyst"], 18, "EU", false),
            (vec!["analyst"], 7, "EU", false),
            (vec!["analyst"], 10, "US", false),
            (vec!["analyst"], 10, "", false),
            (vec!["engineer"], 1, "", true),
            (vec!["engineer", "analyst"], 20, "EU", false),
        ] {
            assert_eq!(allowed(&policy, &roles, hour, region)?, expected);
        }
        Ok(())
    }

    #[test]
    fn global_overnight_restriction_wraps_midnight() -> Result<()> {
        let config = serde_json::from_value(serde_json::json!({
            "roleNames": [], "startHourUtc": 22, "endHourUtc": 6, "regions": []
        }))?;
        let policy = build_general_access_policy(&config)?;
        for (hour, expected) in
            [(21, false), (22, true), (0, true), (5, true), (6, false)]
        {
            assert_eq!(allowed(&policy, &[], hour, "")?, expected);
        }
        Ok(())
    }

    #[test]
    fn invalid_or_ambiguous_guided_rules_are_rejected() -> Result<()> {
        for value in [
            serde_json::json!({}),
            serde_json::json!({"startHourUtc": 9}),
            serde_json::json!({"startHourUtc": 9, "endHourUtc": 9}),
            serde_json::json!({"startHourUtc": 9, "endHourUtc": 25}),
            serde_json::json!({"regions": ["EU\" || true"]}),
            serde_json::json!({"roleNames": ["other/admin"], "regions": ["EU"]}),
        ] {
            let config: GeneralAccessConfig = serde_json::from_value(value)?;
            assert!(build_general_access_policy(&config).is_err());
        }
        assert!(
            serde_json::from_value::<GeneralAccessConfig>(
                serde_json::json!({"startHourUtc": -1, "endHourUtc": 18})
            )
            .is_err()
        );
        assert!(
            serde_json::from_value::<GeneralAccessConfig>(
                serde_json::json!({"unknown": true})
            )
            .is_err()
        );
        Ok(())
    }
}
