//! Emit canonical, independently verifiable blocks in a fixed version.

use std::collections::BTreeMap;

use golc_wire::{
    Block, BlockArgs, Entry, EntryArgs, FlatBufferBuilder, Kind, ScalarTag,
};

use super::container::{
    AUTHORITY, DECLARATIONS, HEADER_LEN, MAX_BLOCK, MAX_LEVEL, PAGE_SIZE,
    Pointer, SCALARS, VERSION, digest, link_key, route_key, target_key,
};
use super::{ArchiveError, ArchivePin, Symbol, encode};
use crate::authorization::{
    Compilation, SecurityEnvironment, Versions, archive as auth, compile,
};
use crate::semantic::{DependencyKind, DependencyTarget, Project};
use crate::{Declaration, FieldKind, FieldRef, Reference, TypeRef};

/// Bytes and authenticated publication metadata from one validated project.
pub struct CompiledArchive {
    bytes: Vec<u8>,
    pin: ArchivePin,
    versions: Versions,
}

impl CompiledArchive {
    /// Borrows the complete artifact for publication or testing.
    #[must_use]
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }

    /// Returns the authenticated root to publish independently of the file.
    #[must_use]
    pub fn pin(&self) -> ArchivePin {
        self.pin
    }

    /// Returns independently pinned revisions for runtime evidence.
    #[must_use]
    pub fn versions(&self) -> &Versions {
        &self.versions
    }
}

/// Compiles a checked project and its security profile into one immutable
/// archive.
///
/// Security is compiled from this exact project, so callers cannot pair
/// permitted routes from one IR with another IR at the same declared version.
pub fn compile_archive(
    project: &Project,
    environment: &SecurityEnvironment,
) -> Result<CompiledArchive, ArchiveError> {
    let contract = match compile(project, environment) {
        Compilation::Validated(contract) => contract,
        Compilation::Invalid(issues) |
        Compilation::PendingExternalValidation(issues) => {
            return Err(ArchiveError::Security(issues));
        },
    };
    let mut writer = Writer {
        bytes: vec![0; HEADER_LEN],
        entries: BTreeMap::new(),
        builder: FlatBufferBuilder::new(),
    };
    writer.add(
        Kind::Authority,
        AUTHORITY.into(),
        &auth::encode_authority(&contract)?,
    )?;
    for (route, descriptor) in contract.routes() {
        writer.add(
            Kind::Route,
            route_key(route),
            &auth::encode_route(descriptor)?,
        )?;
    }
    for (id, module) in project.modules() {
        writer.add(
            Kind::Module,
            format!("m/{id}"),
            &encode(&(
                module.module(),
                module.imports(),
                project.external_imports().get(id),
            ))?,
        )?;
    }
    for (symbol, id) in project.symbols() {
        let definition = project
            .modules()
            .get(symbol.key().ontology())
            .and_then(|ontology| ontology.resolve_local(symbol))
            .ok_or(ArchiveError::Encoding)?;
        let target = Reference::Symbol(symbol.clone());
        let key = target_key(&target);
        writer.add(
            Kind::Definition,
            format!("d/{key}"),
            &encode(&(
                id,
                definition,
                project.effects().get(symbol),
                project.labels().get(symbol),
            ))?,
        )?;
        let kind = definition.kind();
        writer.symbol(Symbol {
            target,
            kind,
            primitive: None,
        })?;
        let fields = match definition {
            Declaration::Entity(entity) => Some(&entity.fields),
            Declaration::Relation(relation) => Some(&relation.fields),
            Declaration::Struct(record) => Some(&record.fields),
            _ => None,
        };
        if let Some(fields) = fields {
            for (name, field) in fields.iter() {
                let ty = match &field.kind {
                    FieldKind::Key(ty) | FieldKind::Value(ty) => Some(ty),
                    FieldKind::OwnedAttribute { .. } => None,
                };
                writer.member(symbol, name, kind, ty)?;
            }
        }
        if let Declaration::View(view) = definition {
            for (name, output) in view.outputs.iter() {
                writer.member(symbol, name, kind, Some(&output.result))?;
            }
        }
        if let Declaration::Relation(relation) = definition {
            for (_, role) in relation.roles.iter() {
                writer.business_link(
                    &Reference::Symbol(symbol.clone()),
                    &Reference::Symbol(role.entity.clone()),
                )?;
            }
        }
    }
    for (name, expression) in project.expressions() {
        writer.add(
            Kind::Definition,
            format!("e/{name}"),
            &encode(expression)?,
        )?;
    }
    for (reference, security) in project.security() {
        writer.add(
            Kind::Definition,
            format!("p/{}", target_key(reference)),
            &encode(security)?,
        )?;
    }

    let mut dependencies = BTreeMap::<_, Vec<_>>::new();
    for edge in project.dependencies() {
        let owner = Reference::Symbol(edge.owner.clone());
        dependencies.entry(&edge.owner).or_default().push(edge);
        if let DependencyTarget::Local(target) = &edge.target {
            let definition = project
                .modules()
                .get(edge.owner.key().ontology())
                .and_then(|module| module.resolve_local(&edge.owner));
            if edge.kind == DependencyKind::Expression {
                // Views and derived fields inherit the very dependencies
                // being indexed. Other declaration-level links may describe
                // a protected member or a private enforcement policy.
                match definition {
                    Some(Declaration::View(_)) => {
                        writer.link(Kind::Dependency, &owner, target)?
                    },
                    Some(Declaration::Derive(value)) => writer.link(
                        Kind::Dependency,
                        &Reference::Field(value.target.clone()),
                        target,
                    )?,
                    _ => {},
                };
            }
        }
    }
    for (owner, dependencies) in dependencies {
        let key = target_key(&Reference::Symbol(owner.clone()));
        writer.add(
            Kind::Dependency,
            format!("q/{key}"),
            &encode(&dependencies)?,
        )?;
    }
    writer.finish(contract.versions().clone())
}

struct Writer {
    bytes: Vec<u8>,
    entries: BTreeMap<String, Pointer>,
    builder: FlatBufferBuilder<'static>,
}

impl Writer {
    fn member(
        &mut self,
        owner: &crate::SymbolRef,
        name: &crate::Name,
        kind: crate::SymbolKind,
        ty: Option<&TypeRef>,
    ) -> Result<(), ArchiveError> {
        let target =
            Reference::Field(FieldRef::new(owner.clone(), name.clone()));
        let mut leaf = ty;
        while let Some(
            TypeRef::Option(inner) | TypeRef::List { element: inner, .. },
        ) = leaf
        {
            leaf = Some(inner);
        }
        // Keep member references anchored to their actual field guard. A
        // resource-level allow must never reveal a protected reference.
        match leaf {
            Some(TypeRef::Entity(symbol)) => self
                .business_link(&target, &Reference::Symbol(symbol.clone()))?,
            Some(TypeRef::Named(symbol)) => self.link(
                Kind::Dependency,
                &target,
                &Reference::Symbol(symbol.clone()),
            )?,
            _ => {},
        }
        let primitive = match ty {
            Some(TypeRef::Primitive(primitive)) => Some(*primitive),
            _ => None,
        };
        self.symbol(Symbol {
            target,
            kind,
            primitive,
        })
    }

    fn business_link(
        &mut self,
        from: &Reference,
        to: &Reference,
    ) -> Result<(), ArchiveError> {
        self.link(Kind::Direct, from, to)?;
        self.link(Kind::Inverse, to, from)?;
        self.link(Kind::Dependency, from, to)
    }

    fn link(
        &mut self,
        kind: Kind,
        from: &Reference,
        to: &Reference,
    ) -> Result<(), ArchiveError> {
        let key = link_key(kind, from, to);
        if self.entries.contains_key(&key) {
            return Ok(());
        }
        // The authenticated record key names both already checked endpoints.
        // Do not copy the private dependency payload into the disclosure
        // index.
        self.add(kind, key, &[])
    }

    fn symbol(&mut self, symbol: Symbol) -> Result<(), ArchiveError> {
        let declaration = DECLARATIONS
            .iter()
            .find_map(|(tag, kind)| (*kind == symbol.kind).then_some(*tag))
            .ok_or(ArchiveError::Encoding)?;
        let scalar = match symbol.primitive {
            None => ScalarTag::Unset,
            Some(primitive) => SCALARS
                .iter()
                .find_map(|(tag, kind)| (*kind == primitive).then_some(*tag))
                .ok_or(ArchiveError::Encoding)?,
        };
        let key = format!("s/{}", target_key(&symbol.target));
        let key_offset = self.builder.create_string(&key);
        let block = Block::create(
            &mut self.builder,
            &BlockArgs {
                kind: Kind::Symbol,
                key: Some(key_offset),
                declaration,
                scalar,
                ..Default::default()
            },
        );
        golc_wire::finish_block_buffer(&mut self.builder, block);
        self.index_record(key)
    }

    fn add(
        &mut self,
        kind: Kind,
        key: String,
        payload: &[u8],
    ) -> Result<(), ArchiveError> {
        if payload.len() > MAX_BLOCK {
            return Err(ArchiveError::Encoding);
        }
        let key_offset = self.builder.create_string(&key);
        let payload = self.builder.create_vector(payload);
        let block = Block::create(
            &mut self.builder,
            &BlockArgs {
                kind,
                key: Some(key_offset),
                payload: Some(payload),
                ..Default::default()
            },
        );
        golc_wire::finish_block_buffer(&mut self.builder, block);
        self.index_record(key)
    }

    fn index_record(&mut self, key: String) -> Result<(), ArchiveError> {
        let pointer = self.flush()?;
        if self.entries.insert(key, pointer).is_some() {
            return Err(ArchiveError::Encoding);
        }
        Ok(())
    }

    fn flush(&mut self) -> Result<Pointer, ArchiveError> {
        let block = self.builder.finished_data();
        if block.len() > MAX_BLOCK {
            return Err(ArchiveError::Encoding);
        }
        let pointer = Pointer {
            offset: self.bytes.len() as u64,
            length: block.len(),
            sha256: digest(block),
        };
        self.bytes.extend_from_slice(block);
        self.builder.reset();
        Ok(pointer)
    }

    fn page(
        &mut self,
        level: u8,
        chunk: &[(String, Pointer)],
    ) -> Result<Pointer, ArchiveError> {
        let mut entries = Vec::with_capacity(chunk.len());
        for (key, pointer) in chunk {
            let key = self.builder.create_string(key);
            let sha256 = self.builder.create_vector(&pointer.sha256);
            let length = pointer
                .length
                .try_into()
                .map_err(|_| ArchiveError::Encoding)?;
            entries.push(Entry::create(
                &mut self.builder,
                &EntryArgs {
                    key: Some(key),
                    offset: pointer.offset,
                    length,
                    sha256: Some(sha256),
                },
            ));
        }
        let entries = self.builder.create_vector(&entries);
        let node = Block::create(
            &mut self.builder,
            &BlockArgs {
                kind: Kind::Directory,
                level,
                entries: Some(entries),
                ..Default::default()
            },
        );
        golc_wire::finish_block_buffer(&mut self.builder, node);
        self.flush()
    }

    fn finish(
        mut self,
        versions: Versions,
    ) -> Result<CompiledArchive, ArchiveError> {
        let mut entries: Vec<_> =
            std::mem::take(&mut self.entries).into_iter().collect();
        let mut level = 0;
        while entries.len() > PAGE_SIZE {
            if level == MAX_LEVEL {
                return Err(ArchiveError::Encoding);
            }
            let mut parents =
                Vec::with_capacity(entries.len().div_ceil(PAGE_SIZE));
            for chunk in entries.chunks(PAGE_SIZE) {
                let (upper, _) = chunk.last().ok_or(ArchiveError::Encoding)?;
                parents.push((upper.clone(), self.page(level, chunk)?));
            }
            entries = parents;
            level += 1;
        }
        let root = self.page(level, &entries)?;
        let all_bytes = self.bytes.len() as u64;
        let root_len =
            u32::try_from(root.length).map_err(|_| ArchiveError::Encoding)?;
        use std::io::Write;
        let header = self
            .bytes
            .get_mut(..HEADER_LEN)
            .ok_or(ArchiveError::Encoding)?;
        let mut cursor = std::io::Cursor::new(header);
        cursor.write_all(b"GOLC")?;
        cursor.write_all(&VERSION.to_le_bytes())?;
        cursor.write_all(&all_bytes.to_le_bytes())?;
        cursor.write_all(&root.offset.to_le_bytes())?;
        cursor.write_all(&root_len.to_le_bytes())?;
        cursor.write_all(&0_u32.to_le_bytes())?;
        cursor.write_all(&root.sha256)?;
        let pin = ArchivePin(digest(cursor.into_inner()));
        Ok(CompiledArchive {
            bytes: self.bytes,
            pin,
            versions,
        })
    }
}
