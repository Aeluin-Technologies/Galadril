//! Real storage effects retain IFC provenance across warm authority checks.

use super::export_fixture as fixture;
#[path = "ifc_fixture.rs"]
mod ifc_fixture;

use std::sync::Arc;

use galadril_ontology::Classification;
use galadril_ontology::archive::remote::{RemoteOptions, RemoteStore};
use galadril_ontology::archive::{ArchiveReader, compile_archive};
use galadril_ontology::authorization::{
    Access, Clearance, Consistency, Evidence, ExecutionContext, Flow,
    InstanceEvidence, Operation, Principal, TrustedRuntime, compile,
};

use super::remote_matrix::rustfs;
use super::{Spice, base, object, start_spice, tuple, update};

struct Calculation<'a> {
    runtime: base::Runtime<'a>,
    context: Arc<ExecutionContext>,
}

impl TrustedRuntime for Calculation<'_> {
    fn computation(
        &self,
        _principal: &Principal,
    ) -> Option<Arc<ExecutionContext>> {
        Some(Arc::clone(&self.context))
    }

    fn verify(&self, access: &Access) -> Option<Evidence> {
        self.runtime.verify(access)
    }
}

fn calculation<'a>(
    runtime: base::Runtime<'a>,
    access: &Access,
    maximum: Classification,
) -> Result<Calculation<'a>, base::TestError> {
    let mut clearance = Clearance::new(maximum);
    clearance.compartments.insert("Legal".parse()?);
    let principal = runtime.verify(access).ok_or("evidence")?.principal;
    let context = Arc::new(ExecutionContext::from_inputs(
        &principal,
        clearance,
        Flow::new(Classification::Public),
    )?);
    Ok(Calculation { runtime, context })
}

async fn value(
    client: &aws_sdk_s3::Client,
) -> Result<String, base::TestError> {
    let object = client
        .get_object()
        .bucket(rustfs::BUCKET)
        .key("public-value")
        .send()
        .await?;
    Ok(String::from_utf8(
        object.body.collect().await?.into_bytes().to_vec(),
    )?)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "runs pinned RustFS and SpiceDB, including an isolated public worker"]
async fn docker_contextual_ifc_guards_committed_effects() -> base::TestResult {
    // The child receives only fixed public inputs and its own address space.
    // No parent data, result, control message or shared runtime is inherited.
    if let Ok(endpoint) = std::env::var("GOL_IFC_PUBLIC_SPICE") {
        let provider = Spice {
            client: reqwest::Client::builder().no_proxy().build()?,
            base: endpoint,
        };
        let project = ifc_fixture::project()?;
        let environment = ifc_fixture::environment(&project)?;
        let compiled = compile(&project, &environment);
        let contract = compiled.publishable().ok_or("contract")?;
        let artifact =
            std::fs::read(std::env::var("GOL_IFC_PUBLIC_ARCHIVE")?)?;
        let archive = compile_archive(&project, &environment)?;
        let reader = ArchiveReader::open(artifact.as_slice(), archive.pin())?;
        let write = base::access(
            &project,
            "Profile",
            Some("a_public"),
            "record",
            Operation::Write,
        )?;
        let calculation = calculation(
            base::runtime(contract),
            &write,
            Classification::Restricted,
        )?;
        let destination = std::env::var("GOL_IFC_PUBLIC_RESULT")?;
        reader
            .perform(&write, &calculation, &provider, || async {
                std::fs::write(destination, "independent public input")
            })
            .await?;
        return Ok(());
    }

    let storage = rustfs::start().await?;
    let (_spice_container, spice) = start_spice().await?;
    let project = ifc_fixture::project()?;
    let environment = ifc_fixture::environment(&project)?;
    let compiled = compile(&project, &environment);
    let contract = compiled
        .publishable()
        .ok_or_else(|| format!("{:?}", compiled.issues()))?;
    let public = base::access(
        &project,
        "Profile",
        Some("a_public"),
        "record",
        Operation::Write,
    )?;
    let secret = base::access(
        &project,
        "Profile",
        Some("z_secret"),
        "record",
        Operation::Read,
    )?;
    let restricted = base::access(
        &project,
        "Profile",
        Some("restricted"),
        "record",
        Operation::Write,
    )?;
    let action =
        base::access(&project, "Validate", None, "job", Operation::Execute)?;
    let record_id = object(&public)?;
    let action_id = object(&action)?;
    let mut grants = Vec::new();
    for id in [&record_id, &action_id] {
        for relation in ["owner", "cleared"] {
            grants.push(update(
                "OPERATION_TOUCH",
                tuple(
                    ("company_record", id),
                    relation,
                    ("user", "tenant_a/owner"),
                    None,
                ),
            ));
        }
    }
    spice.write(grants).await?;
    let archive = compile_archive(&project, &environment)?;
    let store = RemoteStore::connect(
        storage.client.clone(),
        rustfs::BUCKET,
        "tenant_a".parse()?,
        "ifc.contract".parse()?,
        RemoteOptions::default(),
    )
    .await?;
    store.publish(&archive, None).await?;
    let reader = store.open().await?;
    let low =
        calculation(base::runtime(contract), &secret, Classification::Public)?;
    assert!(reader.read(&secret, &low, &spice).await.is_none());
    assert!(low.context.current_label().is_err());

    let directory = tempfile::tempdir()?;
    let artifact = directory.path().join("model.golc");
    let child_result = directory.path().join("independent-public");
    std::fs::write(&artifact, archive.bytes())?;
    // Admission precedes every sensitive observation. The worker cannot
    // receive a secret-dependent scheduling decision from its parent.
    let mut child = std::process::Command::new(std::env::current_exe()?)
        .args([
            "--exact",
            "ifc_matrix::docker_contextual_ifc_guards_committed_effects",
            "--ignored",
        ])
        .env_clear()
        .env("GOL_IFC_PUBLIC_SPICE", &spice.base)
        .env("GOL_IFC_PUBLIC_ARCHIVE", &artifact)
        .env("GOL_IFC_PUBLIC_RESULT", &child_result)
        .spawn()?;
    let high = calculation(
        base::runtime(contract),
        &public,
        Classification::Restricted,
    )?;
    reader
        .perform(&public, &high, &spice, || async {
            storage
                .client
                .put_object()
                .bucket(rustfs::BUCKET)
                .key("public-value")
                .body("public update".as_bytes().to_vec().into())
                .send()
                .await
        })
        .await?;
    assert_eq!(value(&storage.client).await?, "public update");
    assert!(reader.read(&secret, &high, &spice).await.is_some());
    assert_eq!(
        high.context.current_label()?.classification,
        Classification::Confidential
    );
    let inherited = Calculation {
        runtime: high.runtime.clone(),
        context: high.context.fork(&Flow::new(Classification::Public))?,
    };
    assert!(
        reader
            .perform(&public, &inherited, &spice, || async {
                storage
                    .client
                    .delete_object()
                    .bucket(rustfs::BUCKET)
                    .key("public-value")
                    .send()
                    .await
            })
            .await
            .is_err()
    );
    assert_eq!(value(&storage.client).await?, "public update");
    assert!(
        reader
            .perform(&public, &high, &spice, || async {
                storage
                    .client
                    .put_object()
                    .bucket(rustfs::BUCKET)
                    .key("public-value")
                    .body("forbidden copy".as_bytes().to_vec().into())
                    .send()
                    .await
            })
            .await
            .is_err()
    );
    assert_eq!(value(&storage.client).await?, "public update");
    reader
        .perform(&restricted, &high, &spice, || async {
            storage
                .client
                .put_object()
                .bucket(rustfs::BUCKET)
                .key("restricted-value")
                .body("protected update".as_bytes().to_vec().into())
                .send()
                .await
        })
        .await?;
    let committed = storage
        .client
        .get_object()
        .bucket(rustfs::BUCKET)
        .key("restricted-value")
        .send()
        .await?
        .body
        .collect()
        .await?;
    assert_eq!(committed.into_bytes().as_ref(), b"protected update");
    assert!(child.wait()?.success());
    assert_eq!(
        std::fs::read_to_string(child_result)?,
        "independent public input"
    );

    let mut actor = calculation(
        base::runtime(contract),
        &action,
        Classification::Restricted,
    )?;
    actor.runtime.dependencies = Arc::new(
        contract
            .descriptor(&action.route)
            .ok_or("action route")?
            .dependencies()
            .iter()
            .map(|route| {
                let instance = InstanceEvidence {
                    instance: public.instance.clone(),
                    revision: "fixture-v1".into(),
                    flow: None,
                };
                (route.clone(), vec![instance])
            })
            .collect(),
    );
    let forbidden = std::sync::atomic::AtomicBool::new(false);
    assert!(
        reader
            .perform(&action, &actor, &spice, || async {
                forbidden.store(true, std::sync::atomic::Ordering::Relaxed);
                Ok::<_, std::io::Error>(())
            })
            .await
            .is_err()
    );
    assert!(!forbidden.load(std::sync::atomic::Ordering::Relaxed));

    let mut llm = base::access(
        &project,
        "Profile",
        Some("z_secret"),
        "record",
        Operation::DescribeTransfer,
    )?;
    llm.destination = "LLM".parse()?;
    assert!(
        reader
            .perform(&llm, &high, &spice, || async {
                Ok::<_, std::io::Error>(())
            })
            .await
            .is_err()
    );
    let mut kafka = base::access(
        &project,
        "Profile",
        Some("a_public"),
        "record",
        Operation::DescribeTransfer,
    )?;
    kafka.destination = "Kafka".parse()?;
    assert!(
        reader
            .perform(&kafka, &high, &spice, || async {
                Ok::<_, std::io::Error>(())
            })
            .await
            .is_err()
    );

    let mut fresh = calculation(
        base::runtime(contract),
        &public,
        Classification::Restricted,
    )?;
    let mut foreign = base::access(
        &project,
        "Profile",
        Some("a_public"),
        "record",
        Operation::Write,
    )?;
    foreign.instance.tenant = "tenant_b".into();
    assert!(
        reader
            .perform(&foreign, &fresh, &spice, || async {
                storage
                    .client
                    .delete_object()
                    .bucket(rustfs::BUCKET)
                    .key("public-value")
                    .send()
                    .await
            })
            .await
            .is_err()
    );
    let principal = fresh.runtime.verify(&secret).ok_or("evidence")?.principal;
    let no_compartment = Calculation {
        runtime: fresh.runtime.clone(),
        context: Arc::new(ExecutionContext::from_inputs(
            &principal,
            Clearance::new(Classification::Restricted),
            Flow::new(Classification::Public),
        )?),
    };
    assert!(
        reader
            .read(&secret, &no_compartment, &spice)
            .await
            .is_none()
    );
    assert!(
        reader
            .perform(&public, &no_compartment, &spice, || async {
                storage
                    .client
                    .delete_object()
                    .bucket(rustfs::BUCKET)
                    .key("public-value")
                    .send()
                    .await
            })
            .await
            .is_err()
    );
    assert_eq!(value(&storage.client).await?, "public update");
    let receipt = reader
        .perform(&public, &fresh, &spice, || async {
            Ok::<_, std::io::Error>("written")
        })
        .await?;
    assert!(reader.inspect(&receipt, &fresh, &spice).await?.is_ok());
    fresh.runtime.region = "blocked";
    assert!(
        reader
            .perform(&public, &fresh, &spice, || async {
                Ok::<_, std::io::Error>(())
            })
            .await
            .is_err()
    );
    fresh.runtime.region = "EU";
    let revoked = spice
        .write(vec![update(
            "OPERATION_DELETE",
            tuple(
                ("company_record", &record_id),
                "owner",
                ("user", "tenant_a/owner"),
                None,
            ),
        )])
        .await?;
    fresh.runtime.consistency = Consistency::AtLeastAsFresh(revoked);
    assert!(reader.inspect(&receipt, &fresh, &spice).await.is_err());
    assert!(
        reader
            .perform(&public, &fresh, &spice, || async {
                storage
                    .client
                    .delete_object()
                    .bucket(rustfs::BUCKET)
                    .key("public-value")
                    .send()
                    .await
            })
            .await
            .is_err()
    );
    assert_eq!(value(&storage.client).await?, "public update");
    println!(
        "IFC sparse reader: {} bytes, archive {} bytes",
        store.metrics().bytes,
        archive.bytes().len()
    );
    Ok(())
}
