//! Shipped plugin sources use the production GOL compiler and approved
//! profile.

use anyhow::{Context, Result, ensure};
use galadril_ontology::archive::compile_archive;
use galadril_ontology::authorization::compile;
use galadril_ontology::semantic::Environment;
use galadril_registry::gol::{
    GolAuthorityConfig, GolFile, analyze_files, registered_external_types,
};
use sha2::{Digest, Sha256};

const APPROVAL: &str =
    include_str!("../../../infrastructure/gol/authorities.json");
const SCHEMA: &str = include_str!("../../../schemas/spicedb/schema.zed");

const SOURCES: &[(&str, &str)] = &[
    (
        "law.correspondence.gol",
        include_str!(
            "../../../plugins/src/galadril_plugins/law/correspondence.gol"
        ),
    ),
    (
        "communication.delivery.gol",
        include_str!(
            "../../../plugins/src/galadril_plugins/utils/delivery.gol"
        ),
    ),
    (
        "communication.mail.gol",
        include_str!("../../../plugins/src/galadril_plugins/utils/mail.gol"),
    ),
    (
        "law.socle.gol",
        include_str!("../../../plugins/src/galadril_plugins/law/socle.gol"),
    ),
    (
        "law.ecritures.gol",
        include_str!(
            "../../../plugins/src/galadril_plugins/law/ecritures.gol"
        ),
    ),
    (
        "law.moyens.gol",
        include_str!("../../../plugins/src/galadril_plugins/law/moyens.gol"),
    ),
    (
        "law.preuves.gol",
        include_str!("../../../plugins/src/galadril_plugins/law/preuves.gol"),
    ),
    (
        "law.contentieux.gol",
        include_str!(
            "../../../plugins/src/galadril_plugins/law/contentieux.gol"
        ),
    ),
    (
        "law.administratif.gol",
        include_str!(
            "../../../plugins/src/galadril_plugins/law/administratif.gol"
        ),
    ),
    (
        "law.judiciaire.gol",
        include_str!(
            "../../../plugins/src/galadril_plugins/law/judiciaire.gol"
        ),
    ),
    (
        "law.penal.gol",
        include_str!("../../../plugins/src/galadril_plugins/law/penal.gol"),
    ),
];

#[tokio::test]
async fn a_package_cannot_shadow_another_modules_approved_owner() -> Result<()>
{
    use std::sync::Arc;

    use galadril_registry::domain::Registry;
    use galadril_registry::gol_service::{
        GolPublicationConfig, MetadataProvisioner, SaveGolOntology,
    };
    struct UnusedMetadata;
    #[async_trait::async_trait]
    impl MetadataProvisioner for UnusedMetadata {
        async fn provision(
            &self,
            _tenant: &str,
            _ontology: &str,
            _actor: &str,
            _candidates: &[galadril_ontology::SymbolRef],
        ) -> Result<()> {
            anyhow::bail!("unexpected metadata provisioning")
        }
    }
    let client = aws_sdk_s3::Client::from_conf(
        aws_sdk_s3::config::Builder::new()
            .behavior_version_latest()
            .region(aws_sdk_s3::config::Region::new("us-east-1"))
            .build(),
    );
    let config = GolPublicationConfig::new(
        client,
        "ontology-artifacts",
        serde_json::from_str(APPROVAL)?,
        registered_external_types()?,
        Arc::new(UnusedMetadata),
    )?;
    let registry =
        Registry::new(memory::MemoryStore::default()).with_gol(config);
    let workspace=registry.save_gol_ontology(SaveGolOntology {tenant_id:"tenant_a",ontology_id:"workspace.shadow",display_name:"Shadow",files:vec![GolFile {path:"shadow.gol".into(),text:"ontology workspace.shadow version \"1.0.0\" {} ontology law.socle version \"0.4.0\" {}".into()}],author:"alice",expected_revision:None,origin:None}).await?;
    let preview = registry
        .preview_gol_dependencies(
            "tenant_a",
            "workspace.shadow",
            &workspace.summary.source_revision,
            &[],
        )
        .await?;
    let approval = galadril_registry::gol_service::GolCompileApproval {
        plan_pin: preview.plan_pin.context("missing plan pin")?,
        approved_owners: Vec::new(),
    };
    let result = registry
        .validate_gol_ontology(
            "tenant_a",
            "workspace.shadow",
            &workspace.summary.source_revision,
            &approval,
        )
        .await?;
    ensure!(
        result.status == "invalid" &&
            result.diagnostics.iter().any(
                |diagnostic| diagnostic.code == "gol::module_ownership"
            ),
        "companion module shadowing was admitted"
    );
    let consumer=registry.save_gol_ontology(SaveGolOntology {tenant_id:"tenant_a",ontology_id:"workspace.consumer",display_name:"Consumer",files:vec![GolFile {path:"consumer.gol".into(),text:"ontology workspace.consumer version \"1.0.0\" { import communication.people version \"1.0.0\"; }".into()}],author:"alice",expected_revision:None,origin:None}).await?;
    let preview = registry
        .preview_gol_dependencies(
            "tenant_a",
            "workspace.consumer",
            &consumer.summary.source_revision,
            &[],
        )
        .await?;
    ensure!(
        preview.required_owners == vec!["communication.mail"],
        "a companion module did not require its approved source-package owner"
    );
    Ok(())
}

#[path = "support/memory.rs"]
mod memory;

#[test]
fn plugin_sources_are_semantically_and_security_valid_under_real_deployment_contracts()
-> Result<()> {
    let types = registered_external_types()?;
    let files = SOURCES
        .iter()
        .map(|(path, text)| GolFile {
            path: (*path).into(),
            text: (*text).into(),
        })
        .collect::<Vec<_>>();
    let project = analyze_files(&files, &Environment::new(&types)).map_err(
        |errors| {
            anyhow::anyhow!(
                "{}",
                errors
                    .iter()
                    .map(|error| format!(
                        "{}:{}..{} {}",
                        error.path, error.start, error.end, error.message
                    ))
                    .collect::<Vec<_>>()
                    .join("; ")
            )
        },
    )?;
    let authority: GolAuthorityConfig = serde_json::from_str(APPROVAL)?;
    let expected: [u8; 32] = Sha256::digest(SCHEMA.as_bytes()).into();
    ensure!(
        authority.relationship_schema.sha256 == expected,
        "approved SpiceDB snapshot fingerprint differs from deployed schema"
    );
    let environment = authority.environment(&project)?;
    for (module, names) in [
        (
            "communication.delivery",
            &[
                "MailboxAccount",
                "MessageOccurrence",
                "SynchronizationCheckpoint",
                "ReplyDraft",
                "DeliveryReceipt",
            ][..],
        ),
        ("law.ecritures", &["FilingRevision", "Claim"]),
        ("law.moyens", &["Argument"]),
        (
            "law.preuves",
            &["Allegation", "EvidenceAssessment", "JudicialFinding"],
        ),
        (
            "law.contentieux",
            &["ProceduralEvent", "Notification", "DeadlineAssessment"],
        ),
    ] {
        let module = module.parse::<galadril_ontology::OntologyId>()?;
        for name in names {
            let name = name.parse::<galadril_ontology::Name>()?;
            ensure!(
                project
                    .symbols()
                    .keys()
                    .any(|symbol| symbol.key().ontology() == &module &&
                        symbol.key().name() == &name),
                "missing declared business contract {module}::{name}"
            );
        }
    }
    for name in [
        "communication.delivery.ValidMessageUid.condition",
        "communication.delivery.ValidCheckpoint.condition",
        "law.ecritures.PositiveFilingRevision.condition",
    ] {
        ensure!(
            project
                .expressions()
                .keys()
                .any(|key| key.to_string() == name),
            "missing checked invariant {name}"
        );
    }
    let result = compile(&project, &environment);
    let contract = result
        .publishable()
        .ok_or_else(|| anyhow::anyhow!("{:?}", result.issues()))?;
    ensure!(
        contract
            .flows()
            .all(|(_, flow)| authority.authoring_floor.dominates(flow)),
        "source-authoring floor omits a protected plugin flow"
    );
    compile_archive(&project, &environment)?;
    Ok(())
}
