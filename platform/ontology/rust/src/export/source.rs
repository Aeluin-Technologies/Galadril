//! Reuse the same security gate for in-memory and archived descriptions.

use std::collections::BTreeSet;
use std::future::Future;

use super::model::Definition;
use super::{ExportError, is_description};
use crate::authorization::{Access, PermissionProvider, TrustedRuntime};
use crate::{Name, SymbolRef};

/// An explicit field whitelist and the request that authorizes its metadata.
pub struct Selection {
    /// A Describe or DescribeTransfer request bound to an actual context.
    pub access: Access,
    /// Names to consider. Inaccessible and missing names are omitted equally.
    pub fields: BTreeSet<Name>,
}

impl Selection {
    /// Selects candidate fields without claiming any authorization.
    pub fn new(
        access: Access,
        fields: impl IntoIterator<Item = Name>,
    ) -> Result<Self, ExportError> {
        if !is_description(access.route.operation) {
            return Err(ExportError::Unavailable);
        }
        Ok(Self {
            access,
            fields: fields.into_iter().collect(),
        })
    }
}

pub(super) trait Source: Sync {
    fn permitted(
        &self,
        access: &Access,
        runtime: &(impl TrustedRuntime + Sync),
        provider: &(impl PermissionProvider + Sync),
    ) -> impl Future<Output = Result<bool, ExportError>> + Send;

    fn definition(
        &self,
        owner: &SymbolRef,
    ) -> impl Future<Output = Result<Definition, ExportError>> + Send;
}

/// An exporter whose security contract was compiled from the same project.
#[cfg(feature = "parser")]
pub struct ProjectExporter<'a> {
    project: &'a crate::semantic::Project,
    contract: crate::authorization::Contract,
}

#[cfg(feature = "parser")]
impl<'a> ProjectExporter<'a> {
    /// Compiles the mandatory security contracts before exposing metadata.
    pub fn new(
        project: &'a crate::semantic::Project,
        environment: &crate::authorization::SecurityEnvironment,
    ) -> Result<Self, ExportError> {
        match crate::authorization::compile(project, environment) {
            crate::authorization::Compilation::Validated(contract) => {
                Ok(Self {
                    project,
                    contract: *contract,
                })
            },
            _ => Err(ExportError::Unavailable),
        }
    }

    /// Returns the validated contract needed by the trusted runtime loader.
    #[must_use]
    pub fn contract(&self) -> &crate::authorization::Contract {
        &self.contract
    }
}

#[cfg(feature = "parser")]
impl Source for ProjectExporter<'_> {
    async fn permitted(
        &self,
        access: &Access,
        runtime: &(impl TrustedRuntime + Sync),
        provider: &(impl PermissionProvider + Sync),
    ) -> Result<bool, ExportError> {
        Ok(crate::authorization::authorize(
            &self.contract,
            access,
            runtime,
            provider,
        )
        .await ==
            crate::authorization::Decision::Allow)
    }

    async fn definition(
        &self,
        owner: &SymbolRef,
    ) -> Result<Definition, ExportError> {
        let definition = self
            .project
            .modules()
            .get(owner.key().ontology())
            .and_then(|module| module.resolve_local(owner))
            .ok_or(ExportError::Unavailable)?;
        serde_json::from_value(
            serde_json::to_value(definition)
                .map_err(|_| ExportError::Unavailable)?,
        )
        .map_err(|_| ExportError::Unavailable)
    }
}

impl<R: crate::archive::RangeReader + Sync> Source
    for crate::archive::ArchiveReader<R>
{
    async fn permitted(
        &self,
        access: &Access,
        runtime: &(impl TrustedRuntime + Sync),
        provider: &(impl PermissionProvider + Sync),
    ) -> Result<bool, ExportError> {
        self.permitted(access, runtime, provider)
            .await
            .map_err(|_| ExportError::Unavailable)
    }

    async fn definition(
        &self,
        owner: &SymbolRef,
    ) -> Result<Definition, ExportError> {
        self.definition_bytes(owner)
            .await
            .map_err(|_| ExportError::Unavailable)
            .and_then(|bytes| super::model::canonical_definition(&bytes))
    }
}
