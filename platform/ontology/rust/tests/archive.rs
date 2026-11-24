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
fn shared_cedar_actions_retain_each_approved_resource_type()
-> fixture::TestResult {
    let project = fixture::project(0, false)?;
    let mut environment = fixture::environment(&project)?;
    let cedar = environment.cedar.as_mut().ok_or("Cedar authority")?;
    let mut resources = cedar.resources.values_mut();
    let action = resources
        .next()
        .and_then(|resource| resource.actions.first())
        .ok_or("action")?
        .clone();
    resources
        .next()
        .ok_or("second resource")?
        .actions
        .insert(action);
    let compiled = compile(&project, &environment);
    assert!(compiled.publishable().is_some(), "{:?}", compiled.issues());
    Ok(())
}

#[test]
fn archive_authority_pins_are_available_for_independent_host_verification()
-> fixture::TestResult {
    let project = fixture::project(0, false)?;
    let environment = fixture::environment(&project)?;
    let compilation = compile(&project, &environment);
    let contract = compilation.publishable().ok_or("security contract")?;
    assert!(contract.flows().len() > 0);
    assert!(
        contract
            .flows()
            .all(|(target, flow)| contract.flow(target) == Some(flow))
    );
    let archive = compile_archive(&project, &environment)?;
    let reader = ArchiveReader::open(archive.bytes(), archive.pin())?;
    assert_eq!(reader.versions(), contract.versions());
    Ok(())
}

#[tokio::test]
async fn host_requirements_preserve_the_compiled_root_and_prerequisites()
-> fixture::TestResult {
    let project = fixture::project(0, false)?;
    let environment = fixture::environment(&project)?;
    let compilation = compile(&project, &environment);
    let contract = compilation.publishable().ok_or("security contract")?;
    let (route, descriptor) = contract.routes().next().ok_or("route")?;
    let archive = compile_archive(&project, &environment)?;
    let reader = ArchiveReader::open(archive.bytes(), archive.pin())?;
    let requirements = reader.requirements(route).await?;
    assert_eq!(
        requirements.get(route).ok_or("root")?.flow(),
        descriptor.flow()
    );
    assert!(
        descriptor
            .dependencies()
            .iter()
            .all(|route| requirements.contains_key(route))
    );
    Ok(())
}

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

struct Refuse;
impl PermissionProvider for Refuse {
    async fn check(&self, _request: PermissionCheck<'_>) -> PermissionOutcome {
        PermissionOutcome::Deny
    }
}

#[tokio::test]
async fn invariant_contracts_are_explicit_even_without_predicates()
-> fixture::TestResult {
    let project = fixture::project(0, false)?;
    let environment = fixture::environment(&project)?;
    let compilation = compile(&project, &environment);
    let security = compilation.publishable().ok_or("security contract")?;
    let archive = compile_archive(&project, &environment)?;
    let reader = ArchiveReader::open(archive.bytes(), archive.pin())?;
    let request = fixture::access(
        &project,
        "Document",
        None,
        "document",
        Operation::Read,
    )?;
    let contract = reader
        .invariant_contract(&request, &fixture::runtime(security), &All)
        .await
        .ok_or("explicit invariant contract")?;
    assert_eq!(contract.owner(), &request.instance.owner);
    assert!(contract.predicates().is_empty());
    assert!(
        reader
            .invariant_contract(&request, &fixture::runtime(security), &Refuse)
            .await
            .is_none()
    );
    Ok(())
}

#[tokio::test]
async fn trusted_invariant_lookup_preserves_policy_denial_and_outage()
-> fixture::TestResult {
    use galadril_ontology::archive::InvariantContractError;
    use galadril_ontology::authorization::Denial;

    struct Offline;
    impl PermissionProvider for Offline {
        async fn check(
            &self,
            _request: PermissionCheck<'_>,
        ) -> PermissionOutcome {
            PermissionOutcome::Unavailable
        }
    }

    let project = fixture::project(0, false)?;
    let environment = fixture::environment(&project)?;
    let compilation = compile(&project, &environment);
    let security = compilation.publishable().ok_or("security contract")?;
    let archive = compile_archive(&project, &environment)?;
    let reader = ArchiveReader::open(archive.bytes(), archive.pin())?;
    let request = fixture::access(
        &project,
        "Document",
        None,
        "document",
        Operation::Read,
    )?;
    let runtime = fixture::runtime(security);
    assert!(matches!(
        reader
            .invariant_contract_checked(&request, &runtime, &Refuse)
            .await,
        Err(InvariantContractError::Denied(Denial::Relationship))
    ));
    assert!(matches!(
        reader
            .invariant_contract_checked(&request, &runtime, &Offline)
            .await,
        Err(InvariantContractError::Denied(Denial::Unavailable))
    ));
    assert!(
        reader
            .invariant_contract(&request, &runtime, &Offline)
            .await
            .is_none()
    );
    Ok(())
}

#[tokio::test]
async fn invariant_contracts_normalize_aliases_and_reject_tampered_blocks()
-> fixture::TestResult {
    use galadril_ontology::validation::{Bindings, Error, Limits, Scalar};
    use galadril_ontology::{FieldRef, Name, Primitive, TypeRef};

    struct Proposed<'a>(&'a str);
    impl Bindings for Proposed<'_> {
        fn field<'a>(
            &'a self,
            base: Option<&galadril_ontology::checked::Checked>,
            path: &[FieldRef],
            expected: &TypeRef,
        ) -> Result<Scalar<'a>, Error> {
            if base.is_none() &&
                path.len() == 1 &&
                path.first()
                    .is_some_and(|field| field.name.as_str() == "title") &&
                *expected == TypeRef::Primitive(Primitive::String)
            {
                Ok(Scalar::String(self.0))
            } else {
                Err(Error::MissingBinding)
            }
        }

        fn context<'a>(
            &'a self,
            _name: &Name,
            _expected: &TypeRef,
        ) -> Result<Scalar<'a>, Error> {
            Err(Error::MissingBinding)
        }
    }

    let documents = include_str!("fixtures/archive_documents.gol")
        .replace("field title: String", "field title: Title")
        .replace(
            "    entity Document {",
            "    type Title = String;\n    invariant Nonempty on Document { title != \"\" }\n    entity Document {",
        );
    let sources = [
        parse_source("documents.gol", &documents)?,
        parse_source(
            "organization.gol",
            include_str!("fixtures/archive_organization.gol"),
        )?,
        parse_source("mail.gol", include_str!("fixtures/archive_mail.gol"))?,
        parse_source(
            "observations.gol",
            include_str!("fixtures/archive_observations.gol"),
        )?,
    ];
    let registry = ExternalTypeRegistry::default();
    let mut input = Environment::new(&registry);
    for (name, ty) in [
        ("hour", Primitive::I64),
        ("region", Primitive::String),
        ("mfa", Primitive::Bool),
    ] {
        input.context.insert(name.parse()?, ty)?;
    }
    let project = analyze_project(&sources, &input)?;
    let environment = fixture::environment(&project)?;
    let compilation = compile(&project, &environment);
    let security = compilation.publishable().ok_or("security contract")?;
    let archive = compile_archive(&project, &environment)?;
    let trace = Trace::default();
    let reader = ArchiveReader::open(
        Observed(archive.bytes(), trace.clone()),
        archive.pin(),
    )?;
    let request = fixture::access(
        &project,
        "Document",
        None,
        "document",
        Operation::Read,
    )?;
    trace.take()?;
    let contract = reader
        .invariant_contract(&request, &fixture::runtime(security), &All)
        .await
        .ok_or("invariant contract")?;
    assert_eq!(contract.predicates().len(), 1);
    contract.check(&Proposed("allowed"), Limits::default())?;
    assert_eq!(
        contract.check(&Proposed(""), Limits::default()),
        Err(Error::Violation)
    );

    let reads = trace.take()?;
    let (offset, length) = reads.last().copied().ok_or("contract range")?;
    let mut tampered = archive.bytes().to_vec();
    let index = usize::try_from(offset)?
        .checked_add(length.saturating_sub(1))
        .ok_or("contract offset")?;
    let byte = tampered.get_mut(index).ok_or("contract byte")?;
    *byte ^= 1;
    let reader = ArchiveReader::open(tampered.as_slice(), archive.pin())?;
    assert!(
        reader
            .invariant_contract(&request, &fixture::runtime(security), &All)
            .await
            .is_none()
    );
    Ok(())
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
