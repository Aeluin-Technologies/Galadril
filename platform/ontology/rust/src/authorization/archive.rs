//! Preserve decision state without exposing a constructor for validated
//! contracts.

use std::collections::{BTreeMap, BTreeSet};

use cedar_policy::{PolicySet, Schema};
use serde::{Deserialize, Serialize};

use super::contract::{Authority, Descriptor, Permission, Route, Versions};
use super::decision::{authorize_routes, perform_routes};
use super::{
    Access, Decision, EffectError, EffectReceipt, Flow, PermissionProvider,
    Sink, TrustedRuntime,
};
use crate::{AccessBinding, FieldRef, Name, QualifiedName};

type WireError = crate::archive::ArchiveError;

pub(crate) struct ArchivedAuthority(Authority);

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct AuthorityRecord {
    versions: Versions,
    fragments: Vec<Box<str>>,
    policies: serde_json::Value,
    principal: String,
    sinks: BTreeMap<QualifiedName, Sink>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RouteRecord {
    resource: String,
    bindings: Vec<PermissionRecord>,
    flow: Flow,
    dependencies: BTreeSet<Route>,
    fields: BTreeSet<FieldRef>,
    context: BTreeSet<Name>,
    control_flow: Flow,
    compound: bool,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct PermissionRecord {
    name: Name,
    binding: AccessBinding,
    groups: Vec<serde_json::Value>,
}

#[cfg(feature = "parser")]
pub(crate) fn encode_authority(
    contract: &super::Contract,
) -> Result<Vec<u8>, WireError> {
    let authority = &contract.authority;
    crate::archive::encode(&AuthorityRecord {
        versions: authority.versions.clone(),
        fragments: authority.fragments.clone(),
        policies: policy_json(&authority.policies)?,
        principal: authority.principal.to_string(),
        sinks: authority.sinks.clone(),
    })
}

#[cfg(feature = "parser")]
pub(crate) fn encode_route(
    descriptor: &Descriptor,
) -> Result<Vec<u8>, WireError> {
    let bindings = descriptor
        .bindings
        .iter()
        .map(|permission| {
            Ok(PermissionRecord {
                name: permission.name.clone(),
                binding: permission.binding.clone(),
                groups: permission
                    .groups
                    .iter()
                    .map(policy_json)
                    .collect::<Result<_, _>>()?,
            })
        })
        .collect::<Result<_, WireError>>()?;
    crate::archive::encode(&RouteRecord {
        resource: descriptor.resource_type.to_string(),
        bindings,
        flow: descriptor.flow.clone(),
        dependencies: descriptor.dependencies.clone(),
        fields: descriptor.fields.clone(),
        context: descriptor.context.clone(),
        control_flow: descriptor.control_flow.clone(),
        compound: descriptor.compound,
    })
}

#[cfg(feature = "parser")]
fn policy_json(set: &PolicySet) -> Result<serde_json::Value, WireError> {
    // Cedar's JSON EST keeps policy IDs and template definitions. Sorting
    // object keys gives its unordered maps a deterministic wire spelling.
    let mut json = set.clone().to_json().map_err(|_| WireError::Encoding)?;
    json.sort_all_objects();
    Ok(json)
}

impl ArchivedAuthority {
    pub(crate) fn versions(&self) -> &Versions {
        &self.0.versions
    }

    pub(crate) fn decode(bytes: &[u8]) -> Result<Self, WireError> {
        let record: AuthorityRecord = crate::archive::decode(bytes)?;
        let fragments = record
            .fragments
            .iter()
            .map(|text| {
                Schema::from_cedarschema_str(text)
                    .map(|(fragment, _warnings)| fragment)
                    .map_err(|_| WireError::Invalid)
            })
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self(Authority {
            versions: record.versions,
            schema: Schema::schema_link_complete(fragments)
                .map_err(|_| WireError::Invalid)?,
            policies: PolicySet::from_json_value(record.policies)
                .map_err(|_| WireError::Invalid)?,
            principal: record
                .principal
                .parse()
                .map_err(|_| WireError::Invalid)?,
            sinks: record.sinks,
            #[cfg(feature = "parser")]
            fragments: record.fragments,
        }))
    }

    pub(crate) async fn authorize(
        &self,
        access: &Access,
        routes: &BTreeMap<Route, Descriptor>,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Decision {
        authorize_routes(&self.0, routes, access, runtime, provider).await
    }

    pub(crate) async fn perform<
        T,
        E,
        F: std::future::Future<Output = Result<T, E>>,
    >(
        &self,
        access: &Access,
        routes: &BTreeMap<Route, Descriptor>,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
        effect: impl FnOnce() -> F,
    ) -> Result<EffectReceipt<T>, EffectError> {
        perform_routes(&self.0, routes, access, runtime, provider, effect)
            .await
    }

    pub(crate) async fn inspect<'a, T>(
        &self,
        receipt: &'a EffectReceipt<T>,
        routes: &BTreeMap<Route, Descriptor>,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<&'a Result<T, EffectError>, EffectError> {
        receipt
            .inspect_routes(&self.0, routes, runtime, provider)
            .await
    }
}

pub(crate) fn decode_route(bytes: &[u8]) -> Result<Descriptor, WireError> {
    let record: RouteRecord = crate::archive::decode(bytes)?;
    if record.bindings.is_empty() {
        return Err(WireError::Invalid);
    }
    let bindings = record
        .bindings
        .into_iter()
        .map(|permission| {
            let action = permission
                .binding
                .context
                .action
                .parse()
                .map_err(|_| WireError::Invalid)?;
            let groups = permission
                .groups
                .into_iter()
                .map(|group| {
                    PolicySet::from_json_value(group)
                        .map_err(|_| WireError::Invalid)
                })
                .collect::<Result<_, _>>()?;
            Ok(Permission {
                name: permission.name,
                binding: permission.binding,
                action,
                groups,
            })
        })
        .collect::<Result<_, WireError>>()?;
    Ok(Descriptor {
        resource_type: record
            .resource
            .parse()
            .map_err(|_| WireError::Invalid)?,
        bindings,
        flow: record.flow,
        dependencies: record.dependencies,
        fields: record.fields,
        context: record.context,
        control_flow: record.control_flow,
        compound: record.compound,
    })
}
