//! One engine per process prevents native engine globals crossing
//! compilations.
use std::collections::BTreeMap;
use std::io::{Cursor, Read, Seek, SeekFrom, Write};
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use galadril_document_compiler::domain::{
    MAX_PDF_BYTES, MAX_PROJECT_BYTES, Rendered, validate_path,
};
use tectonic::driver::{OutputFormat, ProcessingSessionBuilder};
use tectonic::status::NoopStatusBackend;
use tectonic_bundles::Bundle;
use tectonic_bundles::ttb_fs::TTBFsBundle;
use tectonic_io_base::digest::DigestData;
use tectonic_io_base::{
    InputFeatures, InputHandle, InputOrigin, IoProvider, OpenResult,
};
use tectonic_status_base::StatusBackend;

struct SharedInput(Cursor<Arc<[u8]>>);
impl Read for SharedInput {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        self.0.read(buffer)
    }
}
impl InputFeatures for SharedInput {
    fn get_size(&mut self) -> Result<usize> {
        Ok(self.0.get_ref().len())
    }

    fn try_seek(&mut self, position: SeekFrom) -> Result<u64> {
        Ok(self.0.seek(position)?)
    }
}

struct ProjectBundle {
    files: BTreeMap<String, Arc<[u8]>>,
    bundle: Box<dyn Bundle>,
}
impl IoProvider for ProjectBundle {
    fn input_open_name(
        &mut self,
        name: &str,
        status: &mut dyn StatusBackend,
    ) -> OpenResult<InputHandle> {
        if name.starts_with('/') ||
            name.contains('\\') ||
            name.split('/').any(|part| part == "..")
        {
            return OpenResult::NotAvailable;
        }
        if let Some(data) = self.files.get(name) {
            return OpenResult::Ok(InputHandle::new_read_only(
                name,
                SharedInput(Cursor::new(Arc::clone(data))),
                InputOrigin::Other,
            ));
        }
        self.bundle.input_open_name(name, status)
    }
}
impl Bundle for ProjectBundle {
    fn all_files(&self) -> Vec<String> {
        self.files
            .keys()
            .cloned()
            .chain(self.bundle.all_files())
            .collect()
    }

    fn get_digest(&mut self) -> Result<DigestData> {
        self.bundle.get_digest()
    }
}

/// The worker receives no environment credentials, paths, or remote bundle
/// URLs.
pub fn run() -> Result<()> {
    run_with_status(&mut NoopStatusBackend {})
}

/// Test diagnostics share the exact engine while production suppresses
/// sources.
pub fn run_with_status(status: &mut dyn StatusBackend) -> Result<()> {
    let paths = crate::sandbox_policy::WorkerPaths::from_environment()?;
    crate::kernel::restrict_engine(&paths)?;
    let mut bytes = Vec::new();
    std::io::stdin()
        .take((galadril_document_compiler::domain::MAX_FRAME + 1) as u64)
        .read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() <= galadril_document_compiler::domain::MAX_FRAME,
        "Worker input exceeds limit"
    );
    let project: Rendered = serde_json::from_slice(&bytes)?;
    ensure!(
        project.assets.len() <= 63 &&
            project.entrypoint.len() <= MAX_PROJECT_BYTES,
        "Invalid worker project"
    );
    let mut total = project.entrypoint.len();
    let mut files = BTreeMap::new();
    for asset in project.assets {
        validate_path(&asset.path)?;
        total = total.saturating_add(asset.data.len());
        ensure!(
            total <= MAX_PROJECT_BYTES &&
                files.insert(asset.path, Arc::from(asset.data)).is_none(),
            "Invalid worker assets"
        );
    }
    let bundle = ProjectBundle {
        files,
        bundle: Box::new(TTBFsBundle::new(std::fs::File::open(
            &paths.bundle,
        )?)?),
    };
    let mut builder = ProcessingSessionBuilder::default();
    builder
        .bundle(Box::new(bundle))
        .primary_input_buffer(&project.entrypoint)
        .tex_input_name("main.tex")
        .format_name("latex")
        .filesystem_root(&paths.workspace)
        .format_cache_path(&paths.formats)
        .output_format(OutputFormat::Pdf)
        .do_not_write_output_files()
        .shell_escape_disabled()
        .print_stdout(false)
        .reruns(2)
        .build_date(std::time::UNIX_EPOCH);
    builder
        .hide(&paths.bundle)
        .hide(paths.runtime.join("compiler-worker"));
    let mut session = builder.create(status)?;
    session.run(status)?;
    let outputs = session.into_file_data();
    let pdf = outputs.get("main.pdf").context("Missing PDF output")?;
    ensure!(
        pdf.data.len() <= MAX_PDF_BYTES && pdf.data.starts_with(b"%PDF-"),
        "Invalid PDF output"
    );
    std::io::stdout().write_all(&pdf.data)?;
    Ok(())
}

/// Installs only public runtime files; the init container mounts no service
/// secrets.
pub fn install_rootfs(target: &std::path::Path) -> Result<()> {
    let expected = match std::env::var("DOCUMENT_COMPILER_BUNDLE_SHA256") {
        Ok(value) => value,
        Err(std::env::VarError::NotPresent) => {
            std::fs::read_to_string("/bundle-source/bundle.sha256")?
                .trim()
                .to_owned()
        },
        Err(error) => return Err(error.into()),
    };
    galadril_document_compiler::domain::validate_id(&expected, 64)?;
    ensure!(
        runtime_digest(std::path::Path::new("/bundle-source/bundle.ttb"))?
            .to_string() ==
            expected,
        "Offline bundle digest mismatch"
    );
    std::fs::create_dir_all(target)?;
    for directory in ["lib", "lib64", "usr/lib"] {
        let source = std::path::Path::new("/").join(directory);
        if source.try_exists()? {
            copy_runtime(&source, &target.join(directory))?;
        }
    }
    copy_runtime(&std::env::current_exe()?, &target.join("compiler-worker"))?;
    copy_runtime(
        std::path::Path::new("/bundle-source/bundle.ttb"),
        &target.join("bundle.ttb"),
    )?;
    Ok(())
}

fn copy_runtime(
    source: &std::path::Path,
    destination: &std::path::Path,
) -> Result<()> {
    let metadata = std::fs::symlink_metadata(source)?;
    match std::fs::symlink_metadata(destination) {
        Ok(existing) if metadata.file_type().is_symlink() => {
            ensure!(
                existing.file_type().is_symlink() &&
                    std::fs::read_link(source)? ==
                        std::fs::read_link(destination)?,
                "Public runtime link changed; recreate its volume"
            );
            return Ok(());
        },
        Ok(existing) if metadata.is_file() => {
            ensure!(
                existing.is_file() &&
                    !existing.file_type().is_symlink() &&
                    runtime_digest(source)? == runtime_digest(destination)?,
                "Public runtime file changed; recreate its volume"
            );
            return Ok(());
        },
        Ok(existing) => ensure!(
            existing.is_dir() && !existing.file_type().is_symlink(),
            "Public runtime directory changed"
        ),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {},
        Err(error) => return Err(error.into()),
    }
    if metadata.file_type().is_symlink() {
        let target = std::fs::read_link(source)?;
        if let Some(parent) = destination.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::os::unix::fs::symlink(target, destination)?;
    } else if metadata.is_dir() {
        std::fs::create_dir_all(destination)?;
        for entry in std::fs::read_dir(source)? {
            let entry = entry?;
            copy_runtime(&entry.path(), &destination.join(entry.file_name()))?;
        }
    } else if metadata.is_file() {
        if let Some(parent) = destination.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::copy(source, destination)?;
    } else {
        anyhow::bail!("Unsupported runtime file type");
    }
    Ok(())
}

/// Existing public volumes must match instead of overwriting read-only files.
fn runtime_digest(path: &std::path::Path) -> Result<DigestData> {
    use tectonic_io_base::digest::{Digest, DigestComputer};
    let mut file = std::fs::File::open(path)?;
    ensure!(
        file.metadata()?.len() <= 2 * 1024 * 1024 * 1024,
        "Public runtime file exceeds limit"
    );
    let mut digest = DigestComputer::new();
    let mut buffer = [0_u8; 65536];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(buffer.get(..count).context("Invalid runtime read")?);
    }
    Ok(DigestData::from(digest))
}

/// Readiness validates the exact kernel restrictions in a disposable process.
pub fn probe() -> Result<()> {
    let paths = crate::sandbox_policy::WorkerPaths::from_environment()?;
    crate::kernel::restrict_engine(&paths)?;
    std::io::stdout().write_all(b"ready\n")?;
    Ok(())
}
