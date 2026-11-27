//! Source authoring and catalog discovery must pass independent security
//! gates.

use std::sync::atomic::{AtomicUsize, Ordering};

use anyhow::Result;

use super::*;
use crate::application::ports::control_plane_store::GolFile;
use crate::application::test_support::{
    AuthorizationDecision, TestAuthorization, audit, identity,
};

#[derive(Default)]
struct MemoryStore {
    reads: AtomicUsize,
    saves: AtomicUsize,
    publications: AtomicUsize,
    published: bool,
    imports: bool,
}

fn summary() -> GolSummary {
    GolSummary {
        ontology_id: "communication.mail".into(),
        display_name: "Protected mail".into(),
        source_revision: "a".repeat(40),
        active_generation: None,
        active_source_revision: None,
        status: "draft".into(),
        origin: None,
    }
}

fn workspace() -> GolWorkspace {
    GolWorkspace {
        summary: summary(),
        files: vec![GolFile {
            path: "mail.gol".into(),
            text: "unfinished".into(),
        }],
        diagnostics: Vec::new(),
        dependencies: Vec::new(),
    }
}

#[async_trait::async_trait]
impl ControlPlaneStore for MemoryStore {
    async fn preview_gol_dependencies(
        &self,
        _tenant: &str,
        _id: &str,
        _revision: &str,
        _approved: &[String],
    ) -> Result<galadril_registry::gol_service::GolDependencyPreview> {
        Ok(galadril_registry::gol_service::GolDependencyPreview {
            required_owners: if self.imports {
                vec!["workspace.secret".into()]
            } else {
                Vec::new()
            },
            plan_pin: None,
            dependencies: Vec::new(),
        })
    }

    async fn gol_workspace(
        &self,
        _tenant: &str,
        _id: &str,
    ) -> Result<GolWorkspace> {
        self.reads.fetch_add(1, Ordering::Relaxed);
        Ok(workspace())
    }

    async fn save_gol_ontology(
        &self,
        _tenant: &str,
        _user: &str,
        _input: GolWrite<'_>,
    ) -> Result<GolWorkspace> {
        self.saves.fetch_add(1, Ordering::Relaxed);
        Ok(workspace())
    }

    async fn list_gol_ontologies(
        &self,
        _tenant: &str,
        _limit: usize,
    ) -> Result<Vec<GolSummary>> {
        let mut item = summary();
        if self.published {
            item.active_generation = Some("c".repeat(32));
            item.active_source_revision = Some("b".repeat(40));
        }
        Ok(vec![item])
    }

    async fn publish_gol_ontology(
        &self,
        _tenant: &str,
        _user: &str,
        id: &str,
        revision: &str,
        _expected: Option<&str>,
        _approval: &galadril_registry::gol_service::GolCompileApproval,
    ) -> Result<GolPublication> {
        self.publications.fetch_add(1, Ordering::Relaxed);
        Ok(GolPublication {
            ontology_id: id.into(),
            source_revision: revision.into(),
            generation: "b".repeat(32),
        })
    }

    async fn list_pipeline_executions(
        &self,
        _tenant: &str,
        _pipeline: Option<&str>,
        _limit: usize,
    ) -> Result<Vec<PipelineExecution>> {
        Ok(Vec::new())
    }
}

struct Disclosure(bool);

struct MainSourceOnly;

#[async_trait::async_trait]
impl GolDisclosure for MainSourceOnly {
    async fn admit_creation(
        &self,
        _tenant: &str,
        _user: &str,
        _context: &QueryContext,
        _ontology: &str,
    ) -> Result<()> {
        anyhow::bail!("unused")
    }

    async fn admit_catalog(
        &self,
        _tenant: &str,
        _user: &str,
        _context: &QueryContext,
        _summary: &GolSummary,
    ) -> Result<bool> {
        Ok(false)
    }

    async fn admit_source(
        &self,
        _tenant: &str,
        _user: &str,
        _context: &QueryContext,
        ontology: &str,
        _revision: Option<&str>,
    ) -> Result<()> {
        anyhow::ensure!(
            ontology == "communication.mail",
            "import source denied"
        );
        Ok(())
    }
}

#[tokio::test]
async fn compiling_an_import_cannot_disclose_an_unadmitted_source()
-> Result<()> {
    let store = Arc::new(MemoryStore {
        imports: true,
        ..Default::default()
    });
    let (audit, _) = audit();
    let service = ControlPlaneService::new(
        store.clone(),
        identity(true),
        TestAuthorization::new(AuthorizationDecision::Allow),
        audit,
        Arc::new(MainSourceOnly),
    );
    assert!(
        service
            .publish_gol_ontology(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "communication.mail",
                &"a".repeat(40),
                None
            )
            .await
            .is_err()
    );
    assert_eq!(store.publications.load(Ordering::Relaxed), 0);
    Ok(())
}

#[async_trait::async_trait]
impl GolDisclosure for Disclosure {
    async fn admit_creation(
        &self,
        _tenant: &str,
        _user: &str,
        _context: &QueryContext,
        _ontology: &str,
    ) -> Result<()> {
        anyhow::ensure!(self.0, "creation denied");
        Ok(())
    }

    async fn admit_catalog(
        &self,
        _tenant: &str,
        _user: &str,
        _context: &QueryContext,
        _summary: &GolSummary,
    ) -> Result<bool> {
        Ok(self.0)
    }

    async fn admit_source(
        &self,
        _tenant: &str,
        _user: &str,
        _context: &QueryContext,
        _ontology: &str,
        _revision: Option<&str>,
    ) -> Result<()> {
        anyhow::ensure!(self.0, "source disclosure denied");
        Ok(())
    }
}

struct CreationOnly;

#[async_trait::async_trait]
impl GolDisclosure for CreationOnly {
    async fn admit_creation(
        &self,
        _tenant: &str,
        _user: &str,
        _context: &QueryContext,
        _ontology: &str,
    ) -> Result<()> {
        Ok(())
    }

    async fn admit_catalog(
        &self,
        _tenant: &str,
        _user: &str,
        _context: &QueryContext,
        _summary: &GolSummary,
    ) -> Result<bool> {
        Ok(true)
    }

    async fn admit_source(
        &self,
        _tenant: &str,
        _user: &str,
        _context: &QueryContext,
        _ontology: &str,
        _revision: Option<&str>,
    ) -> Result<()> {
        anyhow::bail!("source denied")
    }
}

#[tokio::test]
async fn creation_right_never_discloses_existing_source_or_draft_metadata()
-> Result<()> {
    let store = Arc::new(MemoryStore::default());
    let (audit, _) = audit();
    let service = ControlPlaneService::new(
        store.clone(),
        identity(true),
        TestAuthorization::new(AuthorizationDecision::Allow),
        audit,
        Arc::new(CreationOnly),
    );
    assert!(
        service
            .gol_workspace(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "communication.mail"
            )
            .await
            .is_err()
    );
    assert_eq!(store.reads.load(Ordering::Relaxed), 0);
    let catalogs = service
        .gol_ontologies("tenant_a", "user_a", &QueryContext::default(), 100)
        .await?;
    assert!(
        catalogs.is_empty(),
        "a draft without an active generation is private source metadata"
    );
    let files = vec![GolFile {
        path: "draft.gol".into(),
        text: "ontology unfinished".into(),
    }];
    service
        .save_gol_ontology(
            "tenant_a",
            "user_a",
            &QueryContext::default(),
            GolWrite {
                ontology_id: "communication.mail",
                display_name: "Mail",
                files: &files,
                expected_revision: None,
                origin: None,
            },
        )
        .await?;
    assert_eq!(store.saves.load(Ordering::Relaxed), 1);
    Ok(())
}

#[tokio::test]
async fn readers_receive_active_publication_pins_without_draft_names_or_revisions()
-> Result<()> {
    let (audit, _) = audit();
    let service = ControlPlaneService::new(
        Arc::new(MemoryStore {
            published: true,
            ..MemoryStore::default()
        }),
        identity(true),
        TestAuthorization::new(AuthorizationDecision::Allow),
        audit,
        Arc::new(CreationOnly),
    );
    let catalog = service
        .gol_ontologies("tenant_a", "user_a", &QueryContext::default(), 100)
        .await?;
    let item = catalog.first().context("active catalog is missing")?;
    assert_eq!(item.display_name, "communication.mail");
    assert_eq!(item.source_revision, "b".repeat(40));
    assert_eq!(item.status, "published");
    Ok(())
}

#[tokio::test]
async fn source_read_requires_edit_and_the_shared_disclosure_gate_before_storage()
-> Result<()> {
    for (deny_edit, disclosure) in [(true, true), (false, false)] {
        let store = Arc::new(MemoryStore::default());
        let auth = TestAuthorization::new(AuthorizationDecision::Allow);
        if deny_edit {
            auth.denied_permissions
                .lock()
                .map_err(|_| anyhow::anyhow!("poisoned test authorization"))?
                .push(Permission::Edit);
        }
        let (audit, _) = audit();
        let service = ControlPlaneService::new(
            store.clone(),
            identity(true),
            auth,
            audit,
            Arc::new(Disclosure(disclosure)),
        );
        assert!(
            service
                .gol_workspace(
                    "tenant_a",
                    "user_a",
                    &QueryContext::default(),
                    "communication.mail"
                )
                .await
                .is_err()
        );
        assert_eq!(store.reads.load(Ordering::Relaxed), 0);
    }
    Ok(())
}

#[tokio::test]
async fn coarse_view_never_discloses_a_catalog_denied_by_ifc() -> Result<()> {
    let (audit, _) = audit();
    let service = ControlPlaneService::new(
        Arc::new(MemoryStore::default()),
        identity(true),
        TestAuthorization::new(AuthorizationDecision::Allow),
        audit,
        Arc::new(Disclosure(false)),
    );
    assert!(
        service
            .gol_ontologies(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                100
            )
            .await?
            .is_empty()
    );
    Ok(())
}

#[tokio::test]
async fn source_saves_require_the_specific_permission_and_do_not_parse_drafts()
-> Result<()> {
    for (permission, expected) in [
        (Permission::CreateOntology, None),
        (
            Permission::Edit,
            Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ),
    ] {
        let store = Arc::new(MemoryStore::default());
        let auth = TestAuthorization::new(AuthorizationDecision::Allow);
        auth.denied_permissions
            .lock()
            .map_err(|_| anyhow::anyhow!("poisoned test authorization"))?
            .push(permission);
        let (audit, _) = audit();
        let service = ControlPlaneService::new(
            store.clone(),
            identity(true),
            auth,
            audit,
            Arc::new(Disclosure(true)),
        );
        let files = vec![GolFile {
            path: "draft.gol".into(),
            text: "ontology unfinished".into(),
        }];
        assert!(
            service
                .save_gol_ontology(
                    "tenant_a",
                    "user_a",
                    &QueryContext::default(),
                    GolWrite {
                        ontology_id: "communication.mail",
                        display_name: "Mail",
                        files: &files,
                        expected_revision: expected,
                        origin: None,
                    }
                )
                .await
                .is_err()
        );
        assert_eq!(store.saves.load(Ordering::Relaxed), 0);
    }
    let store = Arc::new(MemoryStore::default());
    let (audit, _) = audit();
    let service = ControlPlaneService::new(
        store.clone(),
        identity(true),
        TestAuthorization::new(AuthorizationDecision::Allow),
        audit,
        Arc::new(Disclosure(true)),
    );
    let files = vec![GolFile {
        path: "draft.gol".into(),
        text: "ontology unfinished".into(),
    }];
    service
        .save_gol_ontology(
            "tenant_a",
            "user_a",
            &QueryContext::default(),
            GolWrite {
                ontology_id: "communication.mail",
                display_name: "Mail",
                files: &files,
                expected_revision: None,
                origin: None,
            },
        )
        .await?;
    assert_eq!(store.saves.load(Ordering::Relaxed), 1);
    Ok(())
}

#[tokio::test]
async fn publication_denial_has_no_registry_side_effect() -> Result<()> {
    let store = Arc::new(MemoryStore::default());
    let (audit, _) = audit();
    let service = ControlPlaneService::new(
        store.clone(),
        identity(true),
        TestAuthorization::new(AuthorizationDecision::Allow),
        audit,
        Arc::new(Disclosure(false)),
    );
    assert!(
        service
            .publish_gol_ontology(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "communication.mail",
                &"a".repeat(40),
                None
            )
            .await
            .is_err()
    );
    assert_eq!(store.publications.load(Ordering::Relaxed), 0);
    Ok(())
}

#[tokio::test]
async fn management_hints_require_creation_or_source_admission_without_loading_source()
-> Result<()> {
    let store = Arc::new(MemoryStore::default());
    let (audit, _) = audit();
    let service = ControlPlaneService::new(
        store.clone(),
        identity(true),
        TestAuthorization::new(AuthorizationDecision::Allow),
        audit,
        Arc::new(CreationOnly),
    );
    assert!(
        service
            .gol_can_manage(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                None,
                Permission::CreateOntology
            )
            .await?
    );
    assert!(
        !service
            .gol_can_manage(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                Some("communication.mail"),
                Permission::Edit
            )
            .await?
    );
    assert_eq!(store.reads.load(Ordering::Relaxed), 0);
    Ok(())
}
