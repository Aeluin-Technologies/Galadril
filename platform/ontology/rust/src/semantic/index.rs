//! Collection and import resolution build isolated, indexed module scopes.

use std::borrow::Cow;
use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet, HashMap};

use super::diagnostic::{CheckedResult, Code, Diagnostic};
use super::ir::Environment;
use crate::syntax::{self as ast, Declaration as Ast, ImportKind, SyntaxTree};
use crate::{
    ExternalTypeId, Import, ImportSelection, ModuleVersion, Name, Ontology,
    SymbolKind, SymbolRef, Version,
};

pub(super) struct Module<'a> {
    pub file: &'a str,
    pub stamp: ModuleVersion,
    pub ast: &'a ast::Node<ast::Ontology<'a>>,
    pub exports: BTreeMap<&'a str, SymbolRef>,
}

#[derive(Clone, PartialEq, Eq)]
pub(super) enum Target {
    Symbol(SymbolRef),
    External(ExternalTypeId),
}

pub(super) struct Item<'a> {
    pub kind: SymbolKind,
    pub file: &'a str,
    pub node: &'a ast::Node<Ast<'a>>,
}

pub(super) struct Scope<'a> {
    pub module: &'a Module<'a>,
    visible: HashMap<String, (Target, ast::Span)>,
    qualifiers: HashMap<String, Qualifier>,
    pub imports: BTreeMap<crate::OntologyId, Import>,
    pub external: BTreeSet<ExternalTypeId>,
}

struct Qualifier {
    namespace: Box<str>,
    kind: ImportKind,
    span: ast::Span,
}

impl Scope<'_> {
    pub fn error(
        &self,
        span: ast::Span,
        code: Code,
        message: impl Into<Box<str>>,
    ) -> Diagnostic {
        Diagnostic::new(self.module.file, span, code, message)
    }

    pub fn name(&self, node: &ast::Name<'_>) -> CheckedResult<Name> {
        node.value.parse().map_err(|_| {
            self.error(node.span, Code::InvalidName, "Invalid canonical name")
        })
    }

    pub fn find(&self, segments: &[ast::Name<'_>]) -> Option<&Target> {
        self.visible
            .get(spelling(segments).as_ref())
            .map(|(target, _)| target)
    }

    /// One qualifier denotes one namespace even when neither exports a
    /// colliding name. Symbol aliases live in the separate value namespace.
    fn bind_qualifier(
        &mut self,
        name: &str,
        namespace: &str,
        kind: ImportKind,
        span: ast::Span,
    ) -> CheckedResult<()> {
        if let Some(existing) = self.qualifiers.get(name) {
            if existing.namespace.as_ref() == namespace &&
                existing.kind == kind
            {
                return Ok(());
            }
            return Err(self
                .error(
                    span,
                    Code::AmbiguousImport,
                    format!("Conflicting namespace qualifier {name}"),
                )
                .with_related(self.module.file, existing.span));
        }
        self.qualifiers.insert(
            name.into(),
            Qualifier {
                namespace: namespace.into(),
                kind,
                span,
            },
        );
        Ok(())
    }

    fn bind(
        &mut self,
        key: String,
        target: Target,
        span: ast::Span,
    ) -> CheckedResult<()> {
        match self.visible.entry(key) {
            std::collections::hash_map::Entry::Vacant(entry) => {
                entry.insert((target, span));
                Ok(())
            },
            std::collections::hash_map::Entry::Occupied(entry)
                if entry.get().0 == target =>
            {
                Ok(())
            },
            std::collections::hash_map::Entry::Occupied(entry) => {
                Err(Diagnostic::new(
                    self.module.file,
                    span,
                    Code::AmbiguousImport,
                    format!("Ambiguous visible name {}", entry.key()),
                )
                .with_related(self.module.file, entry.get().1))
            },
        }
    }

    pub fn canonical(&self) -> CheckedResult<Ontology> {
        let mut ontology = Ontology::new(self.module.stamp.clone());
        for (id, import) in &self.imports {
            ontology
                .add_import(id.clone(), import.clone())
                .map_err(|e| {
                    self.error(
                        self.module.ast.span,
                        Code::UnresolvedImport,
                        e.to_string(),
                    )
                })?;
        }
        Ok(ontology)
    }
}

pub(super) struct Index<'a> {
    pub modules: BTreeMap<String, Module<'a>>,
    pub items: BTreeMap<SymbolRef, Item<'a>>,
}

pub(super) fn spelling<'a>(segments: &[ast::Name<'a>]) -> Cow<'a, str> {
    match segments {
        [one] => Cow::Borrowed(one.value),
        parts => Cow::Owned(
            parts
                .iter()
                .map(|part| part.value)
                .collect::<Vec<_>>()
                .join("."),
        ),
    }
}

pub(super) fn identify<'a>(
    declaration: &Ast<'a>,
) -> Option<(ast::Name<'a>, SymbolKind)> {
    Some(match declaration {
        Ast::Import(_) | Ast::Access(_) | Ast::Policy(_) => return None,
        Ast::Entity(value) => (value.name.clone(), SymbolKind::Entity),
        Ast::Relation(value) => (value.name.clone(), SymbolKind::Relation),
        Ast::Attribute(value) => (value.name.clone(), SymbolKind::Attribute),
        Ast::TypeAlias(value) => (value.name.clone(), SymbolKind::TypeAlias),
        Ast::Struct(value) => (value.name.clone(), SymbolKind::Struct),
        Ast::Enum(value) => (value.name.clone(), SymbolKind::Enum),
        Ast::Invariant(value) => (value.name.clone(), SymbolKind::Invariant),
        Ast::Derive(value) => (value.name.clone(), SymbolKind::Derive),
        Ast::View(value) => (value.name.clone(), SymbolKind::View),
        Ast::Action(value) => (value.name.clone(), SymbolKind::Action),
        Ast::Label(value) => (value.name.clone(), SymbolKind::IfcLabel),
    })
}

impl<'a> Index<'a> {
    pub fn collect(
        sources: &'a [SyntaxTree<'a>],
        errors: &mut Vec<Diagnostic>,
    ) -> Self {
        let mut index = Self {
            modules: BTreeMap::new(),
            items: BTreeMap::new(),
        };
        let mut order: Vec<_> = sources.iter().collect();
        order.sort_by_key(|tree| (tree.source.name, tree.source.text));
        for source in order {
            for node in &source.modules {
                if let Err(error) =
                    index.module(source.source.name, node, errors)
                {
                    errors.push(error);
                }
            }
        }
        index
    }

    fn module(
        &mut self,
        file: &'a str,
        node: &'a ast::Node<ast::Ontology<'a>>,
        errors: &mut Vec<Diagnostic>,
    ) -> CheckedResult<()> {
        let key = spelling(&node.value.name.value.segments);
        let id = key.parse().map_err(|_| {
            Diagnostic::new(
                file,
                node.value.name.span,
                Code::InvalidName,
                "Invalid ontology name",
            )
        })?;
        let version: Version =
            node.value.version.value.parse().map_err(|_| {
                Diagnostic::new(
                    file,
                    node.value.version.span,
                    Code::VersionConflict,
                    "Expected an exact semantic version",
                )
            })?;
        let entry = match self.modules.entry(key.into_owned()) {
            Entry::Vacant(entry) => entry,
            Entry::Occupied(entry) => {
                let existing = entry.get();
                let code = if existing.stamp.version() == &version {
                    Code::DuplicateSymbol
                } else {
                    Code::VersionConflict
                };
                return Err(Diagnostic::new(
                    file,
                    node.value.name.span,
                    code,
                    format!(
                        "Multiple declarations for ontology {}",
                        entry.key()
                    ),
                )
                .with_related(existing.file, existing.ast.value.name.span));
            },
        };
        let mut module = Module {
            file,
            stamp: ModuleVersion::new(id, version),
            ast: node,
            exports: BTreeMap::new(),
        };
        for declaration in &node.value.declarations {
            let Some((name, kind)) = identify(&declaration.value) else {
                continue;
            };
            let Ok(canonical_name) = name.value.parse::<Name>() else {
                errors.push(Diagnostic::new(
                    file,
                    name.span,
                    Code::InvalidName,
                    "Invalid canonical declaration name",
                ));
                continue;
            };
            let reference = module.stamp.reference(canonical_name);
            match self.items.entry(reference.clone()) {
                Entry::Vacant(symbol) => {
                    symbol.insert(Item {
                        kind,
                        file,
                        node: declaration,
                    });
                    module.exports.insert(name.value, reference);
                },
                Entry::Occupied(symbol) => errors.push(
                    Diagnostic::new(
                        file,
                        name.span,
                        Code::DuplicateSymbol,
                        format!("Duplicate declaration {}", name.value),
                    )
                    .with_related(file, symbol.get().node.span),
                ),
            }
        }
        entry.insert(module);
        Ok(())
    }

    pub fn scopes(
        &self,
        env: &Environment<'_>,
        errors: &mut Vec<Diagnostic>,
    ) -> BTreeMap<crate::OntologyId, Scope<'_>> {
        let mut external: ExternalIndex<'_> = BTreeMap::new();
        for (id, _) in env.external_types.iter() {
            if let Some((namespace, name)) =
                id.name().as_str().rsplit_once('.')
            {
                external
                    .entry(namespace)
                    .or_default()
                    .entry(name)
                    .or_default()
                    .push(id);
            }
        }
        self.modules
            .iter()
            .map(|(key, module)| {
                let mut scope = Scope {
                    module,
                    visible: HashMap::new(),
                    qualifiers: HashMap::from([(
                        key.clone(),
                        Qualifier {
                            namespace: key.as_str().into(),
                            kind: ImportKind::Module,
                            span: module.ast.value.name.span,
                        },
                    )]),
                    imports: BTreeMap::new(),
                    external: BTreeSet::new(),
                };
                for (name, reference) in &module.exports {
                    if let Some(item) = self.items.get(reference) {
                        scope.visible.insert(
                            (*name).into(),
                            (
                                Target::Symbol(reference.clone()),
                                item.node.span,
                            ),
                        );
                        scope.visible.insert(
                            format!("{key}.{name}"),
                            (
                                Target::Symbol(reference.clone()),
                                item.node.span,
                            ),
                        );
                    }
                }
                for declaration in &module.ast.value.declarations {
                    if let Ast::Import(import) = &declaration.value &&
                        let Err(error) = self.resolve_import(
                            &mut scope,
                            &external,
                            import,
                            declaration.span,
                        )
                    {
                        errors.push(error);
                    }
                }
                (module.stamp.ontology().clone(), scope)
            })
            .collect()
    }

    fn resolve_import(
        &self,
        scope: &mut Scope<'_>,
        external: &ExternalIndex<'_>,
        import: &ast::Import<'_>,
        span: ast::Span,
    ) -> CheckedResult<()> {
        let namespace = spelling(&import.module.value.segments);
        let pin = import
            .version
            .as_ref()
            .map(|version| {
                version.value.parse::<Version>().map_err(|_| {
                    scope.error(
                        version.span,
                        Code::VersionConflict,
                        "Expected an exact semantic version",
                    )
                })
            })
            .transpose()?;
        let selected: Option<&[_]> = match &import.selection {
            ast::ImportSelection::All => None,
            ast::ImportSelection::Symbols(names) => Some(names),
        };
        let alias = import.alias.as_ref().map(|name| name.value);
        if alias.is_some() && selected.is_some_and(|names| names.len() != 1) {
            return Err(scope.error(
                span,
                Code::UnsupportedSemanticConstruct,
                "An alias requires a module or a single symbol",
            ));
        }
        scope.bind_qualifier(&namespace, &namespace, import.kind, span)?;
        if let Some(alias) = alias.filter(|_| selected.is_none()) {
            scope.bind_qualifier(alias, &namespace, import.kind, span)?;
        }
        let mut targets = BTreeMap::new();
        match import.kind {
            ImportKind::Module => {
                let module =
                    self.modules.get(namespace.as_ref()).ok_or_else(|| {
                        scope.error(
                            span,
                            Code::UnresolvedImport,
                            format!("Missing module {namespace}"),
                        )
                    })?;
                if pin
                    .as_ref()
                    .is_some_and(|version| version != module.stamp.version())
                {
                    return Err(scope
                        .error(
                            span,
                            Code::VersionConflict,
                            format!(
                                "Incompatible import version for {namespace}"
                            ),
                        )
                        .with_related(
                            module.file,
                            module.ast.value.version.span,
                        ));
                }
                for name in selection(
                    scope,
                    span,
                    selected,
                    module.exports.keys().copied(),
                )? {
                    let reference =
                        module.exports.get(name).ok_or_else(|| {
                            scope.error(
                                span,
                                Code::UnresolvedImport,
                                format!("Unknown export {namespace}.{name}"),
                            )
                        })?;
                    targets.insert(name, Target::Symbol(reference.clone()));
                }
                let canonical = if selected.is_some() {
                    let names = targets
                        .values()
                        .filter_map(|target| match target {
                            Target::Symbol(reference) => {
                                Some(reference.key().name().clone())
                            },
                            Target::External(_) => None,
                        })
                        .collect();
                    ImportSelection::Only(names)
                } else {
                    ImportSelection::All
                };
                match scope.imports.entry(module.stamp.ontology().clone()) {
                    Entry::Vacant(entry) => {
                        entry.insert(Import::new(
                            module.stamp.version().clone(),
                            canonical,
                        ));
                    },
                    Entry::Occupied(mut entry) => merge_selection(
                        &mut entry.get_mut().selection,
                        canonical,
                    ),
                }
            },
            ImportKind::ExternalType => {
                let group =
                    external.get(namespace.as_ref()).ok_or_else(|| {
                        scope.error(
                            span,
                            Code::UnresolvedImport,
                            format!("Missing external namespace {namespace}"),
                        )
                    })?;
                for name in
                    selection(scope, span, selected, group.keys().copied())?
                {
                    let id = external_version(
                        scope,
                        span,
                        &namespace,
                        name,
                        group,
                        pin.as_ref(),
                    )?;
                    scope.external.insert(id.clone());
                    targets.insert(name, Target::External(id.clone()));
                }
            },
        }
        for (name, target) in targets {
            scope.bind(format!("{namespace}.{name}"), target.clone(), span)?;
            if let Some(prefix) = alias.filter(|_| selected.is_none()) {
                scope.bind(format!("{prefix}.{name}"), target, span)?;
            } else {
                scope.bind(alias.unwrap_or(name).into(), target, span)?;
            }
        }
        Ok(())
    }
}

type ExternalIndex<'a> =
    BTreeMap<&'a str, BTreeMap<&'a str, Vec<&'a ExternalTypeId>>>;

fn selection<'a>(
    scope: &Scope<'_>,
    span: ast::Span,
    selected: Option<&[ast::Name<'a>]>,
    all: impl Iterator<Item = &'a str>,
) -> CheckedResult<BTreeSet<&'a str>> {
    let Some(selected) = selected else {
        return Ok(all.collect());
    };
    let mut names = BTreeSet::new();
    for name in selected {
        if !names.insert(name.value) {
            return Err(scope
                .error(
                    name.span,
                    Code::DuplicateSymbol,
                    "Duplicate import selection",
                )
                .with_related(scope.module.file, span));
        }
    }
    Ok(names)
}

fn external_version<'a>(
    scope: &Scope<'_>,
    span: ast::Span,
    namespace: &str,
    name: &str,
    group: &BTreeMap<&str, Vec<&'a ExternalTypeId>>,
    pin: Option<&Version>,
) -> CheckedResult<&'a ExternalTypeId> {
    let mut candidates = group
        .get(name)
        .into_iter()
        .flatten()
        .copied()
        .filter(|id| pin.is_none_or(|version| version == id.version()));
    let first = candidates.next().ok_or_else(|| scope.error(span, Code::UnresolvedImport, format!("No registered descriptor for {namespace}.{name} at the requested version")))?;
    if candidates.next().is_some() {
        return Err(scope.error(
            span,
            Code::AmbiguousImport,
            format!("Pin a descriptor version for {namespace}.{name}"),
        ));
    }
    Ok(first)
}

fn merge_selection(old: &mut ImportSelection, new: ImportSelection) {
    match (old, new) {
        (slot, ImportSelection::All) => *slot = ImportSelection::All,
        (ImportSelection::Only(old), ImportSelection::Only(new)) => {
            old.extend(new)
        },
        (ImportSelection::All, ImportSelection::Only(_)) => {},
    }
}

pub(super) fn resolve_symbol(
    scope: &Scope<'_>,
    index: &Index<'_>,
    names: &[ast::Name<'_>],
    span: ast::Span,
    code: Code,
    allowed: impl FnOnce(SymbolKind) -> bool,
) -> CheckedResult<SymbolRef> {
    if let Some(Target::Symbol(reference)) = scope.find(names) &&
        index
            .items
            .get(reference)
            .is_some_and(|item| allowed(item.kind))
    {
        return Ok(reference.clone());
    }
    Err(scope.error(
        span,
        code,
        format!("No compatible symbol named {}", spelling(names)),
    ))
}
