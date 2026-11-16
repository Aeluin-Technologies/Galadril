//! The local archive must preserve validated metadata and enforce every read.

use std::error::Error;

use galadril_ontology::archive::{ArchiveError, compile_archive};
use galadril_ontology::authorization::{
    AuthorityRevision, Flow, SecurityEnvironment,
};
use galadril_ontology::semantic::{Environment, analyze_project};
use galadril_ontology::syntax::parse_source;
use galadril_ontology::{
    Classification, ExternalTypeRegistry, Version, VersionedName,
};

#[test]
fn publication_rejects_missing_security_authorities()
-> Result<(), Box<dyn Error>> {
    let source = parse_source(
        "local.gol",
        "ontology local version \"1.0.0\" { entity Item { field title: String; } }",
    )?;
    let registry = ExternalTypeRegistry::default();
    let project = analyze_project(&[source], &Environment::new(&registry))?;
    let revision = AuthorityRevision::new(
        VersionedName::new("local.deployment".parse()?, Version::new(1, 0, 0)),
        [3; 32],
    );
    let environment = SecurityEnvironment::new(
        revision,
        Flow::new(Classification::Internal),
    );
    assert!(matches!(
        compile_archive(&project, &environment),
        Err(ArchiveError::Security(_))
    ));
    Ok(())
}

#[path = "support/archive_fixture.rs"]
mod fixture;

use std::io;
use std::sync::{Arc, Mutex};

use galadril_ontology::archive::{
    ArchivePin, ArchiveReader, Direction, LocalFile, RangeReader,
};
use galadril_ontology::authorization::{
    Decision, Operation, PermissionCheck, PermissionOutcome,
    PermissionProvider, authorize, compile,
};
use sha2::{Digest, Sha256};

#[test]
fn deserialization_preserves_canonical_identifier_validation() {
    use galadril_ontology::{Name, QualifiedName};

    assert!(serde_json::from_str::<Name>(r#""invalid!""#).is_err());
    assert!(
        serde_json::from_str::<QualifiedName>(r#""empty..segment""#).is_err()
    );
}

#[derive(Default, Clone)]
struct Trace(Arc<Mutex<Vec<(u64, usize)>>>);

impl Trace {
    fn take(&self) -> Result<Vec<(u64, usize)>, io::Error> {
        Ok(std::mem::take(
            &mut *self.0.lock().map_err(|_| io::Error::other("trace lock"))?,
        ))
    }
}

struct Observed<R>(R, Trace);

impl<R: RangeReader> RangeReader for Observed<R> {
    fn length(&self) -> io::Result<u64> {
        self.0.length()
    }

    fn read_exact_at(&self, offset: u64, buffer: &mut [u8]) -> io::Result<()> {
        self.1
            .0
            .lock()
            .map_err(|_| io::Error::other("trace lock"))?
            .push((offset, buffer.len()));
        self.0.read_exact_at(offset, buffer)
    }
}

struct All;
impl PermissionProvider for All {
    async fn check(&self, request: PermissionCheck<'_>) -> PermissionOutcome {
        PermissionOutcome::Allow(request.consistency.clone())
    }
}

#[tokio::test]
async fn sparse_local_queries_match_the_memory_gate_at_cold_and_warm_cache()
-> fixture::TestResult {
    let project = fixture::project(640, false)?;
    let environment = fixture::environment(&project)?;
    let compilation = compile(&project, &environment);
    let contract = compilation
        .publishable()
        .ok_or_else(|| format!("{:?}", compilation.issues()))?;
    let runtime = fixture::runtime(contract);
    let archive = compile_archive(&project, &environment)?;
    let reordered = fixture::project(640, true)?;
    let other =
        compile_archive(&reordered, &fixture::environment(&reordered)?)?;
    assert_eq!(archive.pin(), other.pin());
    assert!(archive.bytes() == other.bytes());

    let directory = tempfile::tempdir()?;
    let path = directory.path().join("example.golc");
    std::fs::write(&path, archive.bytes())?;
    let trace = Trace::default();
    let reader = ArchiveReader::open(
        Observed(LocalFile::open(path)?, trace.clone()),
        archive.pin(),
    )?;
    let opened = trace.take()?;
    let access = fixture::access(
        &project,
        "Document",
        Some("title"),
        "d1",
        Operation::Read,
    )?;
    let expected = authorize(contract, &access, &runtime, &All).await;
    let result = reader
        .read(&access, &runtime, &All)
        .await
        .ok_or("missing title")?;
    assert_eq!(expected, Decision::Allow);
    assert_eq!(result.target, access.route.target);
    assert_eq!(result.primitive, Some(galadril_ontology::Primitive::String));
    let cold = trace.take()?;
    assert_eq!(reader.read(&access, &runtime, &All).await, Some(result));
    let warm = trace.take()?;
    let size = |ranges: &[(u64, usize)]| {
        ranges.iter().map(|(_, length)| length).sum::<usize>()
    };
    assert!(size(&opened) + size(&cold) < archive.bytes().len() / 10);
    assert!(size(&warm) < size(&cold));
    assert!(opened.iter().chain(&cold).chain(&warm).all(
        |(offset, length)| {
            *offset + *length as u64 <= archive.bytes().len() as u64
        }
    ));
    println!(
        "GOLC bytes={} open={} ranges/{}, cold={} ranges/{}, warm={} ranges/{}, cold_offsets={cold:?}",
        archive.bytes().len(),
        opened.len(),
        size(&opened),
        cold.len(),
        size(&cold),
        warm.len(),
        size(&warm)
    );
    Ok(())
}

#[tokio::test]
async fn corruption_and_undisclosed_targets_leave_no_query_payload()
-> fixture::TestResult {
    let project = fixture::project(128, false)?;
    let deployment = fixture::environment(&project)?;
    let compilation = compile(&project, &deployment);
    let contract = compilation
        .publishable()
        .ok_or_else(|| format!("{:?}", compilation.issues()))?;
    let runtime = fixture::runtime(contract);
    let archive = compile_archive(&project, &deployment)?;
    let valid = archive.bytes();
    assert!(ArchiveReader::open(valid, ArchivePin([0; 32])).is_err());
    assert!(
        ArchiveReader::open(
            valid.get(..valid.len() - 1).ok_or("fixture bytes")?,
            archive.pin()
        )
        .is_err()
    );

    let mut bad_bounds = valid.to_vec();
    bad_bounds
        .get_mut(16..24)
        .ok_or("header offset")?
        .copy_from_slice(&u64::MAX.to_le_bytes());
    let bad_pin = ArchivePin(
        Sha256::digest(bad_bounds.get(..64).ok_or("header")?).into(),
    );
    assert!(ArchiveReader::open(bad_bounds.as_slice(), bad_pin).is_err());

    let mut bad_root = valid.to_vec();
    *bad_root.last_mut().ok_or("root payload")? ^= 1;
    assert!(ArchiveReader::open(bad_root.as_slice(), archive.pin()).is_err());

    // The trusted publication pin retains the original per-record digest
    // even when a plausible payload is altered independently.
    let mut bad_symbol = valid.to_vec();
    let key = b"s/company.documents|1.0.0|Document|title";
    let offset = bad_symbol
        .windows(key.len())
        .position(|window| window == key)
        .ok_or("symbol block")?;
    *bad_symbol.get_mut(offset).ok_or("symbol byte")? ^= 1;
    let reader = ArchiveReader::open(bad_symbol.as_slice(), archive.pin())?;
    let title = fixture::access(
        &project,
        "Document",
        Some("title"),
        "d1",
        Operation::Read,
    )?;
    assert_eq!(reader.read(&title, &runtime, &All).await, None);
    let organization = fixture::access(
        &project,
        "Organization",
        None,
        "org1",
        Operation::Read,
    )?;
    assert!(reader.read(&organization, &runtime, &All).await.is_some());

    let mut bad_route = valid.to_vec();
    let route = b"r/r/company.documents|1.0.0|Document|title";
    let offset = bad_route
        .windows(route.len())
        .position(|window| window == route)
        .ok_or("route block")?;
    *bad_route.get_mut(offset).ok_or("route byte")? ^= 1;
    let reader = ArchiveReader::open(bad_route.as_slice(), archive.pin())?;
    assert_eq!(reader.read(&title, &runtime, &All).await, None);

    let trace = Trace::default();
    let reader =
        ArchiveReader::open(Observed(valid, trace.clone()), archive.pin())?;
    let denied = fixture::Runtime {
        mfa: false,
        ..runtime.clone()
    };
    let secret = fixture::access(
        &project,
        "Document",
        Some("body"),
        "d1",
        Operation::Read,
    )?;
    let unknown = fixture::access(
        &project,
        "Document",
        Some("absent"),
        "d1",
        Operation::Read,
    )?;
    assert_eq!(
        reader.read(&secret, &denied, &All).await,
        reader.read(&unknown, &denied, &All).await
    );
    let hidden_key = b"s/company.documents|1.0.0|Document|body";
    let hidden_offset = valid
        .windows(hidden_key.len())
        .position(|window| window == hidden_key)
        .ok_or("protected symbol block")? as u64;
    let contains_hidden_byte = |ranges: &[(u64, usize)]| {
        ranges.iter().any(|(start, length)| {
            (*start..(*start + *length as u64)).contains(&hidden_offset)
        })
    };
    assert!(!contains_hidden_byte(&trace.take()?));
    assert!(reader.read(&secret, &runtime, &All).await.is_some());
    assert!(contains_hidden_byte(&trace.take()?));
    let missing_contract =
        fixture::access(&project, "Entry0000", None, "e1", Operation::Read)?;
    let visible = reader
        .discover(&[secret, unknown, missing_contract, title], &denied, &All)
        .await
        .ok_or("discovery")?;
    assert_eq!(visible.len(), 1);
    let attached =
        fixture::access(&project, "Attached", None, "a1", Operation::Read)?;
    let document =
        fixture::access(&project, "Document", None, "d1", Operation::Read)?;
    assert_eq!(
        reader
            .links(&attached, &[document], Direction::Direct, &denied, &All)
            .await
            .ok_or("direct")?
            .len(),
        1
    );
    let message =
        fixture::access(&project, "Message", None, "m1", Operation::Read)?;
    let document =
        fixture::access(&project, "Document", None, "d1", Operation::Read)?;
    let endpoints = [document];
    assert_eq!(
        reader
            .links(&message, &endpoints, Direction::Direct, &denied, &All)
            .await,
        Some(Vec::new()),
    );
    let member = fixture::access(
        &project,
        "Message",
        Some("attachment"),
        "m1",
        Operation::Read,
    )?;
    assert_eq!(
        reader
            .links(&member, &endpoints, Direction::Direct, &denied, &All)
            .await
            .ok_or("field link")?
            .len(),
        1,
    );
    Ok(())
}
