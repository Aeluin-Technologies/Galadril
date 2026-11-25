//! Verifies immutable publication and tenant isolation at the Registry
//! boundary.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, ensure};
use async_trait::async_trait;
use galadril_registry::domain::{PutPipeline, Registry};
use galadril_registry::storage::{RevisionStore, Snapshot, TenantSnapshot};
use serde_json::json;

#[derive(Default)]
struct MemoryStore {
    tenants: Mutex<BTreeMap<String, Vec<StoredSnapshot>>>,
    fail_next_commit: Arc<AtomicBool>,
}

struct StoredSnapshot {
    revision_id: String,
    state_json: Vec<u8>,
}

#[tokio::test]
async fn pipeline_edits_preserve_an_immutable_revision_chain() -> Result<()> {
    let registry = Registry::new(MemoryStore::default());
    let first = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "mail",
            name: "v1",
            owner_id: "owner",
            definition: definition("v1"),
            author: "owner",
            message: "Initial",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    ensure!(first.record.previous_revision_id.is_none());
    let edited = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "mail",
            name: "v2",
            owner_id: "owner",
            definition: definition("v2"),
            author: "owner",
            message: "Second",
            expected_revision_id: Some(&first.head_revision_id),
            create_only: false,
        })
        .await?;
    ensure!(
        edited.record.previous_revision_id.as_deref() ==
            Some(first.head_revision_id.as_str())
    );
    let previous = registry
        .pipeline_revision("tenant_a", "mail", &first.head_revision_id)
        .await?;
    ensure!(previous.record.definition.get("name") == Some(&json!("v1")));
    Ok(())
}

#[async_trait]
impl RevisionStore for MemoryStore {
    async fn list_tenants(
        &self,
        limit: u32,
        page_token: Option<&str>,
    ) -> anyhow::Result<galadril_registry::storage::TenantPage> {
        let tenants = self
            .tenants
            .lock()
            .map_err(|_| anyhow::anyhow!("memory store lock poisoned"))?;
        let mut ids = tenants
            .keys()
            .filter(|key| page_token.is_none_or(|token| key.as_str() > token))
            .take(usize::try_from(limit)? + 1)
            .cloned()
            .collect::<Vec<_>>();
        let next_page_token = if ids.len() > usize::try_from(limit)? {
            ids.pop();
            ids.last().cloned()
        } else {
            None
        };
        Ok(galadril_registry::storage::TenantPage {
            tenant_ids: ids,
            next_page_token,
        })
    }

    async fn tenant_exists(&self, tenant: &str) -> Result<bool> {
        let tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        Ok(tenants.contains_key(tenant))
    }

    async fn ensure_tenant(&self, tenant: &str) -> Result<TenantSnapshot> {
        let mut tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        let snapshots = tenants.entry(tenant.to_owned()).or_default();
        Ok(TenantSnapshot {
            revision_id: snapshots.last().map_or_else(
                || "00000000000000000000".to_owned(),
                |snapshot| snapshot.revision_id.clone(),
            ),
        })
    }

    async fn delete_tenant(&self, tenant: &str) -> Result<()> {
        let mut tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        tenants.remove(tenant).context("tenant unavailable")?;
        Ok(())
    }

    async fn load(
        &self,
        tenant: &str,
        revision: Option<&str>,
    ) -> Result<Snapshot> {
        let tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        let snapshots = tenants.get(tenant).context("tenant unavailable")?;
        let stored = if let Some(revision) = revision {
            Some(
                snapshots
                    .iter()
                    .find(|item| item.revision_id == revision)
                    .context("revision unavailable")?,
            )
        } else {
            snapshots.last()
        };
        let Some(stored) = stored else {
            return Ok(Snapshot {
                revision_id: "00000000000000000000".to_owned(),
                state: Default::default(),
            });
        };
        Ok(Snapshot {
            revision_id: stored.revision_id.clone(),
            state: serde_json::from_slice(&stored.state_json)?,
        })
    }

    async fn commit(
        &self,
        tenant: &str,
        expected: &str,
        state: &galadril_registry::state::TenantState,
        _author: &str,
        _message: &str,
    ) -> Result<String> {
        ensure!(
            !self.fail_next_commit.swap(false, Ordering::AcqRel),
            "injected commit failure"
        );
        let mut tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        let snapshots = tenants.entry(tenant.to_owned()).or_default();
        let current = snapshots
            .last()
            .map_or("00000000000000000000", |item| item.revision_id.as_str());
        ensure!(current == expected, "registry revision changed");
        let revision = format!("{:020}", snapshots.len() + 1);
        snapshots.push(StoredSnapshot {
            revision_id: revision.clone(),
            state_json: serde_json::to_vec(state)?,
        });
        Ok(revision)
    }
}

fn definition(name: &str) -> serde_json::Value {
    json!({
        "name": name,
        "sources": [{"id":"source"}],
        "pipeline": [{"step":"sink","type":"sink","input_from":["source"]}]
    })
}

#[test]
fn branch_deletion_is_an_explicit_authoring_command() -> Result<()> {
    let command: galadril_registry::authoring::AuthoringCommand =
        serde_json::from_value(
            json!({"operation":"delete_branch","branch":"feature"}),
        )?;
    ensure!(
        serde_json::to_value(command)? ==
            json!({"operation":"delete_branch","branch":"feature"})
    );
    Ok(())
}

#[tokio::test]
async fn branch_deletion_protects_main_descendants_and_open_proposals()
-> Result<()> {
    use galadril_registry::authoring::AuthoringCommand;
    let registry = Registry::new(MemoryStore::default());
    let definition = json!({"name":"Mail","sources":[{"id":"source","topic":"raw","match_pattern":".*","schema_path":"text.avsc"}],"pipeline":[{"step":"sink","type":"sink","input_from":["source"]}]});
    let mut revision: Option<String> = None;
    let commands = [
        (json!({"operation":"save_draft","branch":"main"}), true, 0),
        (
            json!({"operation":"create_branch","branch":"feature","from_branch":"main"}),
            true,
            1,
        ),
        (
            json!({"operation":"delete_branch","branch":"main"}),
            false,
            1,
        ),
        (
            json!({"operation":"create_branch","branch":"child","from_branch":"feature"}),
            true,
            2,
        ),
        (
            json!({"operation":"delete_branch","branch":"feature"}),
            false,
            2,
        ),
        (
            json!({"operation":"delete_branch","branch":"child"}),
            true,
            1,
        ),
        (
            json!({"operation":"propose_merge","source_branch":"feature"}),
            true,
            1,
        ),
        (
            json!({"operation":"delete_branch","branch":"feature"}),
            false,
            1,
        ),
        (
            json!({"operation":"merge","proposal_id":"feature","resolutions":{}}),
            true,
            1,
        ),
        (
            json!({"operation":"delete_branch","branch":"feature"}),
            true,
            0,
        ),
    ];
    for (encoded, accepted, remaining) in commands {
        let command: AuthoringCommand = serde_json::from_value(encoded)?;
        let result = registry
            .author_pipeline(
                PutPipeline {
                    tenant_id: "tenant_a",
                    pipeline_id: "mail",
                    name: "Mail",
                    owner_id: "owner",
                    definition: definition.clone(),
                    author: "owner",
                    message: "Review branches",
                    expected_revision_id: revision.as_deref(),
                    create_only: revision.is_none(),
                },
                command,
            )
            .await;
        if accepted {
            let saved = result?;
            ensure!(saved.record.authoring.branches.len() == remaining);
            revision = Some(saved.head_revision_id);
        } else {
            ensure!(result.is_err());
        }
    }
    Ok(())
}

#[tokio::test]
async fn runtime_expands_pinned_transfers_without_following_new_heads()
-> Result<()> {
    let registry = Registry::new(MemoryStore::default());
    let child = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "child",
            name: "Child",
            owner_id: "owner",
            definition: definition("original"),
            author: "author",
            message: "child",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    registry
        .publish_pipeline("tenant_a", "child", &child.head_revision_id)
        .await?;
    let authored = json!({"name":"root", "sources":[{"id":"input"}], "pipeline":[{"step":"send", "type":"pipeline", "input_from":["input"], "pipeline_ref":{"pipeline_id":"child","revision_id":child.head_revision_id,"input_source":"source"}}]});
    let root = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "root",
            name: "Root",
            owner_id: "owner",
            definition: authored,
            author: "author",
            message: "compose",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    registry
        .publish_pipeline("tenant_a", "root", &root.head_revision_id)
        .await?;
    let current = registry.current_pipeline("tenant_a", "child").await?;
    registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "child",
            name: "Child",
            owner_id: "owner",
            definition: definition("changed"),
            author: "author",
            message: "new child",
            expected_revision_id: Some(&current.head_revision_id),
            create_only: false,
        })
        .await?;
    let expanded = registry
        .runtime_pipeline("tenant_a", "root", &root.head_revision_id)
        .await?;
    let steps = expanded
        .definition
        .get("pipeline")
        .and_then(serde_json::Value::as_array)
        .context("steps")?;
    ensure!(steps.len() == 2);
    ensure!(
        steps
            .iter()
            .any(|step| step.pointer("/origin/pipeline_id") ==
                Some(&json!("child")) &&
                step.pointer("/origin/revision_id") ==
                    Some(&json!(child.head_revision_id)))
    );
    ensure!(
        registry
            .runtime_pipeline("tenant_b", "root", &root.head_revision_id)
            .await
            .is_err()
    );
    Ok(())
}

#[tokio::test]
async fn publication_pins_an_immutable_pipeline_revision_per_tenant()
-> Result<()> {
    let registry = Registry::new(MemoryStore::default());
    let first = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("v1"),
            author: "author",
            message: "create",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    ensure!(
        registry
            .runtime_pipeline("tenant_a", "daily", &first.head_revision_id)
            .await
            .is_err(),
        "draft must be unavailable to runtime"
    );
    registry
        .publish_pipeline("tenant_a", "daily", &first.head_revision_id)
        .await?;
    let current = registry.current_pipeline("tenant_a", "daily").await?;
    let updated = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("v2"),
            author: "author",
            message: "update",
            expected_revision_id: Some(&current.head_revision_id),
            create_only: false,
        })
        .await?;

    let published = registry
        .runtime_pipeline("tenant_a", "daily", &first.head_revision_id)
        .await?;
    assert_eq!(published.definition.get("name"), Some(&json!("v1")));
    assert_eq!(updated.record.definition.get("name"), Some(&json!("v2")));
    ensure!(
        registry
            .runtime_pipeline("tenant_a", "daily", &updated.head_revision_id)
            .await
            .is_err(),
        "a new draft must not inherit the previous publication"
    );
    registry
        .delete_pipeline("tenant_a", "daily", &updated.head_revision_id)
        .await?;
    ensure!(
        registry
            .runtime_pipeline("tenant_a", "daily", &first.head_revision_id)
            .await
            .is_err(),
        "removing the deployment must revoke runtime discovery"
    );
    assert!(
        registry
            .runtime_pipeline("tenant_b", "daily", &first.head_revision_id)
            .await
            .is_err()
    );
    Ok(())
}

#[tokio::test]
async fn stale_pipeline_update_fails_closed() -> Result<()> {
    let registry = Registry::new(MemoryStore::default());
    let first = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("v1"),
            author: "author",
            message: "create",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    let stale = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("v2"),
            author: "author",
            message: "update",
            expected_revision_id: Some("99999999999999999999"),
            create_only: false,
        })
        .await;
    assert!(stale.is_err());
    assert_eq!(
        registry
            .current_pipeline("tenant_a", "daily")
            .await?
            .head_revision_id,
        first.head_revision_id
    );
    Ok(())
}

#[tokio::test]
async fn authoring_saves_incomplete_drafts_without_activating_them()
-> Result<()> {
    use galadril_registry::authoring::AuthoringCommand;
    let registry = Registry::new(MemoryStore::default());
    let draft = registry.author_pipeline(PutPipeline {
        tenant_id: "tenant_a", pipeline_id: "mail", name: "Mail", owner_id: "owner",
        definition: json!({"name":"Mail","sources":[],"pipeline":[{"step":"reply","type":"sink","input_from":[]}]}),
        author: "owner", message: "Draft", expected_revision_id: None, create_only: true,
    }, AuthoringCommand::SaveDraft { branch: "main".to_owned() }).await?;
    ensure!(draft.record.published_revision_id.is_none());
    ensure!(
        registry
            .author_pipeline(
                PutPipeline {
                    tenant_id: "tenant_a",
                    pipeline_id: "mail",
                    name: "Mail",
                    owner_id: "owner",
                    definition: json!({}),
                    author: "owner",
                    message: "Approve",
                    expected_revision_id: Some(&draft.head_revision_id),
                    create_only: false,
                },
                AuthoringCommand::Approve
            )
            .await
            .is_err()
    );
    ensure!(
        registry
            .list_published_pipelines("tenant_a", 100)
            .await?
            .is_empty()
    );
    Ok(())
}

#[tokio::test]
async fn authoring_branches_merge_by_block_and_require_review_before_deployment()
-> Result<()> {
    use galadril_registry::authoring::AuthoringCommand;
    let registry = Registry::new(MemoryStore::default());
    let mut current = registry.author_pipeline(PutPipeline {
        tenant_id: "tenant_a", pipeline_id: "mail", name: "Mail", owner_id: "owner",
        definition: json!({"name":"Mail","sources":[{"id":"source","topic":"raw","match_pattern":".*","schema_path":"text.avsc"}],"pipeline":[{"step":"sink","type":"sink","input_from":["source"]}]}), author: "owner", message: "Draft", expected_revision_id: None, create_only: true,
    }, AuthoringCommand::SaveDraft { branch: "main".to_owned() }).await?;
    for command in [
        AuthoringCommand::CreateBranch {
            branch: "feature".to_owned(),
            from_branch: "main".to_owned(),
        },
        AuthoringCommand::CreateBranch {
            branch: "nested".to_owned(),
            from_branch: "feature".to_owned(),
        },
        AuthoringCommand::ProposeMerge {
            source_branch: "feature".to_owned(),
        },
        AuthoringCommand::Merge {
            proposal_id: "feature".to_owned(),
            resolutions: BTreeMap::new(),
        },
        AuthoringCommand::Approve,
        AuthoringCommand::Publish,
    ] {
        current = registry
            .author_pipeline(
                PutPipeline {
                    tenant_id: "tenant_a",
                    pipeline_id: "mail",
                    name: "Mail",
                    owner_id: "owner",
                    definition: json!({}),
                    author: "owner",
                    message: "Author",
                    expected_revision_id: Some(&current.head_revision_id),
                    create_only: false,
                },
                command,
            )
            .await?;
        ensure!(current.record.published_revision_id.is_none());
    }
    let encoded = serde_json::to_value(&current.record.authoring)?;
    ensure!(
        encoded.pointer("/branches/feature/parent_branch") ==
            Some(&json!("main"))
    );
    ensure!(
        encoded.pointer("/branches/nested/parent_branch") ==
            Some(&json!("feature"))
    );
    let published = current
        .record
        .authoring
        .release
        .as_ref()
        .context("release missing")?
        .revision_id
        .clone();
    ensure!(
        registry
            .runtime_pipeline("tenant_a", "mail", &published)
            .await
            .is_err(),
        "approval and publication must not replace explicit deployment"
    );
    current = registry
        .author_pipeline(
            PutPipeline {
                tenant_id: "tenant_a",
                pipeline_id: "mail",
                name: "Mail",
                owner_id: "owner",
                definition: json!({}),
                author: "owner",
                message: "Deploy",
                expected_revision_id: Some(&current.head_revision_id),
                create_only: false,
            },
            AuthoringCommand::Deploy,
        )
        .await?;
    ensure!(
        current.record.published_revision_id.as_deref() ==
            Some(published.as_str())
    );
    registry
        .runtime_pipeline("tenant_a", "mail", &published)
        .await?;
    ensure!(
        registry
            .list_published_pipelines("tenant_a", 100)
            .await?
            .len() ==
            1
    );
    let stale = registry
        .author_pipeline(
            PutPipeline {
                tenant_id: "tenant_a",
                pipeline_id: "mail",
                name: "Mail",
                owner_id: "owner",
                definition: definition("stale"),
                author: "owner",
                message: "stale",
                expected_revision_id: Some(&published),
                create_only: false,
            },
            AuthoringCommand::SaveDraft {
                branch: "main".to_owned(),
            },
        )
        .await;
    ensure!(stale.is_err());
    Ok(())
}

#[test]
fn pipeline_merges_preserve_independent_blocks_and_report_delete_edit_conflicts()
-> Result<()> {
    use galadril_registry::authoring::{MergeChoice, merge_definitions};
    let base = json!({"name":"Mail","sources":[{"id":"mail"}],"pipeline":[{"step":"a","type":"sink","input_from":["mail"]},{"step":"b","type":"sink","input_from":["mail"]}]});
    let left = json!({"name":"Mail","sources":[{"id":"mail"}],"pipeline":[{"step":"a","type":"sink","input_from":["mail"],"params":{"limit":1}},{"step":"b","type":"sink","input_from":["mail"]}]});
    let right = json!({"name":"Mail","sources":[{"id":"mail"}],"pipeline":[{"step":"a","type":"sink","input_from":["mail"]},{"step":"b","type":"sink","input_from":["mail"],"params":{"limit":2}}]});
    let merged = merge_definitions(&base, &left, &right, &BTreeMap::new())?;
    ensure!(merged.conflicts.is_empty());
    ensure!(
        merged.definition.pointer("/pipeline/0/params/limit") ==
            Some(&json!(1))
    );
    ensure!(
        merged.definition.pointer("/pipeline/1/params/limit") ==
            Some(&json!(2))
    );
    let deleted = json!({"name":"Mail","sources":[{"id":"mail"}],"pipeline":[{"step":"b","type":"sink","input_from":["mail"]}]});
    let conflict =
        merge_definitions(&base, &deleted, &left, &BTreeMap::new())?;
    ensure!(conflict.conflicts.len() == 1);
    let path = conflict
        .conflicts
        .first()
        .context("missing conflict")?
        .path
        .clone();
    let resolved = merge_definitions(
        &base,
        &deleted,
        &left,
        &BTreeMap::from([(path, MergeChoice::Source)]),
    )?;
    ensure!(resolved.conflicts.is_empty());
    ensure!(
        resolved.definition.pointer("/pipeline/0/params/limit") ==
            Some(&json!(1))
    );
    Ok(())
}

#[test]
fn incomplete_configuration_cannot_be_approved() -> Result<()> {
    use galadril_registry::authoring::validate_executable;
    let incomplete = json!({"name":"Draft","sources":[{"id":"mail"}],"pipeline":[{"step":"send","type":"sink","input_from":[]}]});
    ensure!(validate_executable(&incomplete).is_err());
    Ok(())
}

#[tokio::test]
async fn legacy_publication_cannot_bypass_managed_pipeline_approval()
-> Result<()> {
    use galadril_registry::authoring::AuthoringCommand;
    let registry = Registry::new(MemoryStore::default());
    let draft = registry.author_pipeline(PutPipeline {
        tenant_id: "tenant_a", pipeline_id: "managed", name: "Mail", owner_id: "owner",
        definition: json!({"name":"Mail","sources":[{"id":"mail","topic":"raw","match_pattern":".*","schema_path":"email.avsc"}],"pipeline":[]}),
        author: "owner", message: "draft", expected_revision_id: None, create_only: true,
    }, AuthoringCommand::SaveDraft { branch: "main".to_owned() }).await?;
    ensure!(
        registry
            .publish_pipeline("tenant_a", "managed", &draft.head_revision_id)
            .await
            .is_err()
    );
    Ok(())
}

#[test]
fn authoritative_diagnostics_localize_incomplete_blocks() -> Result<()> {
    let issues = galadril_registry::authoring::diagnose_definition(
        &json!({"name":"Draft","sources":[],"pipeline":[{"step":"model","type":"inference","input_from":[]}]}),
    )?;
    ensure!(issues.iter().any(|issue| issue.code == "input" &&
        issue.node_id.as_deref() == Some("model")));
    ensure!(issues.iter().any(|issue| issue.code == "model" &&
        issue.node_id.as_deref() == Some("model")));
    Ok(())
}

#[test]
fn scheduled_pipeline_starts_without_an_event_source() -> Result<()> {
    use galadril_registry::authoring::validate_executable;
    let definition = json!({
        "name": "Scheduled",
        "sources": [],
        "pipeline": [
            {"step": "timer", "type": "causal", "input_from": [],
             "params": {"trigger":"cron", "cron":"0 8 * * 1", "amarth_target_outcome":"case_count"}},
            {"step":"output", "type":"sink", "input_from":["timer"]}
        ]
    });
    validate_executable(&definition)
}

#[test]
fn linked_pipeline_cannot_return_to_an_older_revision_of_the_root() {
    use std::collections::BTreeMap;

    use galadril_registry::composition::compile;
    let a = "a".repeat(32);
    let b = "b".repeat(32);
    let c = "c".repeat(32);
    let transfer = |pipeline: &str, revision: &str| {
        json!({
            "name": "Pipeline", "sources": [{"id": "input"}],
            "pipeline": [{"step": "transfer", "type": "pipeline", "input_from": ["input"],
                "pipeline_ref": {"pipeline_id":pipeline, "revision_id":revision, "input_source":"input"}}]
        })
    };
    let old =
        json!({"name":"Old root", "sources":[{"id":"input"}], "pipeline":[]});
    let linked = BTreeMap::from([
        (("child".to_owned(), b.clone()), transfer("root", &a)),
        (("root".to_owned(), a), old),
    ]);
    let result = compile("root", &c, &transfer("child", &b), &linked);
    assert!(result.is_err_and(|error| error.to_string().contains("cycle")));
}
