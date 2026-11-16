//! FlatBuffer verification and authenticated range addressing.

use golc_wire::{Block, DeclarationTag, Entry, ScalarTag};
use sha2::{Digest, Sha256};

use super::{ArchiveError, ArchivePin};
use crate::{Primitive, SymbolKind};

pub(super) const HEADER_LEN: usize = 64;
pub(super) const VERSION: u32 = 1;
pub(super) const PAGE_SIZE: usize = 32;
pub(super) const MAX_BLOCK: usize = 8 * 1024 * 1024;
pub(super) const MAX_LEVEL: u8 = 7;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) struct Pointer {
    pub offset: u64,
    pub length: usize,
    pub sha256: [u8; 32],
}

impl Pointer {
    pub fn entry(entry: Entry<'_>, parent: u64) -> Result<Self, ArchiveError> {
        let pointer = Self {
            offset: entry.offset(),
            length: entry
                .length()
                .try_into()
                .map_err(|_| ArchiveError::Invalid)?,
            sha256: entry
                .sha256()
                .bytes()
                .try_into()
                .map_err(|_| ArchiveError::Invalid)?,
        };
        pointer.validate(parent)?;
        Ok(pointer)
    }

    fn validate(self, bound: u64) -> Result<(), ArchiveError> {
        if self.length < 8 ||
            self.length > MAX_BLOCK ||
            self.offset < HEADER_LEN as u64 ||
            self.offset
                .checked_add(self.length as u64)
                .is_none_or(|end| end > bound)
        {
            return Err(ArchiveError::Invalid);
        }
        Ok(())
    }
}

pub(super) fn digest(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

pub(super) fn header(
    bytes: &[u8; HEADER_LEN],
    length: u64,
    pin: ArchivePin,
) -> Result<Pointer, ArchiveError> {
    if digest(bytes) != pin.0 {
        return Err(ArchiveError::Invalid);
    }
    use std::io::{Cursor, Read};
    let mut fields = Cursor::new(bytes);
    let mut magic = [0; 4];
    fields.read_exact(&mut magic)?;
    let read = |fields: &mut Cursor<_>| -> Result<u32, ArchiveError> {
        let mut buf = [0; 4];
        fields.read_exact(&mut buf)?;
        Ok(u32::from_le_bytes(buf))
    };
    if &magic != b"GOLC" || read(&mut fields)? != VERSION {
        return Err(ArchiveError::Version);
    }
    let mut size = [0; 8];
    fields.read_exact(&mut size)?;
    if u64::from_le_bytes(size) != length {
        return Err(ArchiveError::Invalid);
    }
    let mut offset = [0; 8];
    fields.read_exact(&mut offset)?;
    let root_len = read(&mut fields)? as usize;
    if read(&mut fields)? != 0 {
        return Err(ArchiveError::Version);
    }
    let mut sha256 = [0; 32];
    fields.read_exact(&mut sha256)?;
    let pointer = Pointer {
        offset: u64::from_le_bytes(offset),
        length: root_len,
        sha256,
    };
    pointer.validate(length)?;
    if pointer.offset + pointer.length as u64 != length {
        return Err(ArchiveError::Invalid);
    }
    Ok(pointer)
}

pub(super) fn block(bytes: &[u8]) -> Result<Block<'_>, ArchiveError> {
    // The identifier helper itself assumes an eight-byte prefix.
    if bytes.len() < 8 || !golc_wire::block_buffer_has_identifier(bytes) {
        return Err(ArchiveError::Invalid);
    }
    golc_wire::root_as_block(bytes).map_err(|_| ArchiveError::Invalid)
}

pub(super) fn target_key(target: &crate::Reference) -> String {
    let (owner, member) = match target {
        crate::Reference::Symbol(owner) => (owner, None),
        crate::Reference::Field(field) => (&field.owner, Some(&field.name)),
    };
    // GOL identifiers and SemVer exclude the separator.
    format!(
        "{}|{}|{}|{}",
        owner.key().ontology(),
        owner.version(),
        owner.key().name(),
        member.map_or("", crate::Name::as_str),
    )
}

pub(super) fn route_key(route: &crate::authorization::Route) -> String {
    use crate::authorization::Operation;
    let operation = match route.operation {
        Operation::Read => 'r',
        Operation::Write => 'w',
        Operation::Execute => 'x',
        Operation::Transfer => 't',
        Operation::Describe => 'd',
        Operation::DescribeTransfer => 's',
    };
    format!("r/{operation}/{}", target_key(&route.target))
}

pub(super) fn link_key(
    kind: golc_wire::Kind,
    anchor: &crate::Reference,
    candidate: &crate::Reference,
) -> String {
    format!(
        "{}/{}/{}",
        kind.0,
        target_key(anchor),
        target_key(candidate)
    )
}

pub(super) const AUTHORITY: &str = "authority";

// Keep wire tags independent of the declaration order of the Rust enums.
pub(super) const DECLARATIONS: &[(DeclarationTag, SymbolKind)] = &[
    (DeclarationTag::Entity, SymbolKind::Entity),
    (DeclarationTag::Relation, SymbolKind::Relation),
    (DeclarationTag::Attribute, SymbolKind::Attribute),
    (DeclarationTag::TypeAlias, SymbolKind::TypeAlias),
    (DeclarationTag::Struct, SymbolKind::Struct),
    (DeclarationTag::Enum, SymbolKind::Enum),
    (DeclarationTag::Invariant, SymbolKind::Invariant),
    (DeclarationTag::Derive, SymbolKind::Derive),
    (DeclarationTag::View, SymbolKind::View),
    (DeclarationTag::Action, SymbolKind::Action),
    (DeclarationTag::IfcLabel, SymbolKind::IfcLabel),
];

pub(super) const SCALARS: &[(ScalarTag, Primitive)] = &[
    (ScalarTag::Unit, Primitive::Unit),
    (ScalarTag::Bool, Primitive::Bool),
    (ScalarTag::Char, Primitive::Char),
    (ScalarTag::String, Primitive::String),
    (ScalarTag::I8, Primitive::I8),
    (ScalarTag::I16, Primitive::I16),
    (ScalarTag::I32, Primitive::I32),
    (ScalarTag::I64, Primitive::I64),
    (ScalarTag::I128, Primitive::I128),
    (ScalarTag::U8, Primitive::U8),
    (ScalarTag::U16, Primitive::U16),
    (ScalarTag::U32, Primitive::U32),
    (ScalarTag::U64, Primitive::U64),
    (ScalarTag::U128, Primitive::U128),
    (ScalarTag::F32, Primitive::F32),
    (ScalarTag::F64, Primitive::F64),
];
