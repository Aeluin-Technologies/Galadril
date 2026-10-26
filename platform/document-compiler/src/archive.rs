//! Stored ZIP entries bound CPU and memory and cannot name external paths.
use std::collections::BTreeSet;
use std::io::{Cursor, Write};

use anyhow::{Result, ensure};
use zip::write::SimpleFileOptions;
use zip::{CompressionMethod, ZipWriter};

use crate::domain::{MAX_PDF_BYTES, validate_path};

pub struct Archive {
    writer: ZipWriter<Cursor<Vec<u8>>>,
    names: BTreeSet<String>,
    size: usize,
}
impl Default for Archive {
    fn default() -> Self {
        Self::new()
    }
}
impl Archive {
    pub fn new() -> Self {
        Self {
            writer: ZipWriter::new(Cursor::new(Vec::new())),
            names: BTreeSet::new(),
            size: 0,
        }
    }

    pub fn add(&mut self, name: &str, data: &[u8]) -> Result<()> {
        validate_path(name)?;
        ensure!(
            self.names.len() < 64 && !self.names.contains(name),
            "Duplicate or excessive archive entries"
        );
        let size = self
            .size
            .saturating_add(data.len())
            .saturating_add(name.len() * 2 + 256);
        ensure!(size <= MAX_PDF_BYTES, "Archive exceeds limit");
        self.writer.start_file(
            name,
            SimpleFileOptions::default()
                .compression_method(CompressionMethod::Stored)
                .unix_permissions(0o600),
        )?;
        self.writer.write_all(data)?;
        self.names.insert(name.into());
        self.size = size;
        Ok(())
    }

    pub fn finish(self) -> Result<Vec<u8>> {
        ensure!(!self.names.is_empty(), "Archive cannot be empty");
        let bytes = self.writer.finish()?.into_inner();
        ensure!(bytes.len() <= MAX_PDF_BYTES, "Archive exceeds limit");
        Ok(bytes)
    }
}
