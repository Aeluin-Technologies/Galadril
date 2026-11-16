//! Module-level imports, declaration scope, and exact local lookups.

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};

use crate::declaration::Declaration;
use crate::error::ModelError;
use crate::identity::{
    ModuleVersion, Name, OntologyId, OntologyVersion, SymbolRef,
};
use crate::named::Named;

/// The names exposed by a pinned module import.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum ImportSelection {
    /// Imports the whole module scope.
    All,
    /// Imports only the listed local declaration names.
    Only(BTreeSet<Name>),
}

/// A declared dependency, keyed by the target ontology in [`Ontology`].
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Import {
    /// The exact version selected by the caller or a future lock resolver.
    pub version: OntologyVersion,
    /// The names requested from that version.
    pub selection: ImportSelection,
}

impl Import {
    /// Records an exact dependency and its selection.
    #[must_use]
    pub fn new(version: OntologyVersion, selection: ImportSelection) -> Self {
        Self { version, selection }
    }
}

/// An independently constructed GOL module.
///
/// Construction checks scope uniqueness. Reference validity, cross-module
/// composition, and security validation require a later semantic pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ontology {
    module: ModuleVersion,
    imports: BTreeMap<OntologyId, Import>,
    declarations: Named<Declaration>,
}

impl Ontology {
    /// Starts an empty module in the given logical scope and version.
    #[must_use]
    pub fn new(module: ModuleVersion) -> Self {
        Self {
            module,
            imports: BTreeMap::new(),
            declarations: Named::new(),
        }
    }

    /// Returns the scope and version that local references must match.
    #[must_use]
    pub fn module(&self) -> &ModuleVersion {
        &self.module
    }

    /// Borrows imports in canonical ontology-name order.
    #[must_use]
    pub fn imports(&self) -> &BTreeMap<OntologyId, Import> {
        &self.imports
    }

    /// Adds a dependency without altering an existing version pin.
    ///
    /// # Errors
    ///
    /// Returns [`ModelError::DuplicateImport`] for another import of the same
    /// module. A future resolver can combine source imports before insertion.
    pub fn add_import(
        &mut self,
        ontology: OntologyId,
        import: Import,
    ) -> Result<(), ModelError> {
        match self.imports.entry(ontology) {
            Entry::Vacant(entry) => {
                entry.insert(import);
                Ok(())
            },
            Entry::Occupied(entry) => Err(ModelError::DuplicateImport {
                ontology: entry.key().clone(),
            }
            .record()),
        }
    }

    /// Borrows the module's shared declaration scope.
    #[must_use]
    pub fn declarations(&self) -> &Named<Declaration> {
        &self.declarations
    }

    /// Adds a top-level declaration to the shared scope.
    ///
    /// # Errors
    ///
    /// Returns [`ModelError::DuplicateName`] if any declaration has this name.
    pub fn declare(
        &mut self,
        name: Name,
        declaration: Declaration,
    ) -> Result<(), ModelError> {
        self.declarations.insert(name, declaration)
    }

    /// Looks up a symbol only when its module and exact version match.
    ///
    /// The caller checks the returned declaration's kind for the use site.
    #[must_use]
    pub fn resolve_local(
        &self,
        reference: &SymbolRef,
    ) -> Option<&Declaration> {
        if reference.key().ontology() != self.module.ontology() ||
            reference.version() != self.module.version()
        {
            return None;
        }
        self.declarations.get(reference.key().name().as_str())
    }
}
