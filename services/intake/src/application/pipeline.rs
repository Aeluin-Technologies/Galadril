//! Pure async system file discovery and pipeline structure parsing.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use config::{Config, File, FileFormat};

use crate::domain::models::PipelineConfig;

/// Parses raw YAML payloads into structured pipeline models.
pub fn parse_tenant_pipeline(yaml_content: &str) -> Result<PipelineConfig> {
    let builder = Config::builder()
        .add_source(File::from_str(yaml_content, FileFormat::Yaml));

    let built_config = builder.build().context(
        "Failed to build dynamic config-rs layer for tenant pipeline",
    )?;

    built_config
        .try_deserialize()
        .context("Failed to deserialize PipelineConfig from YAML buffer")
}

/// Discovers all Avro schema files and reads them completely asynchronously.
pub async fn discover_local_schemas(
    root_dir: impl AsRef<Path>,
) -> Result<Vec<(PathBuf, String)>> {
    let root_path = root_dir.as_ref();
    let root_canonical = root_path.canonicalize().with_context(|| {
        format!("failed to resolve root dir path: {:?}", root_path)
    })?;

    let mut discovered = Vec::new();
    let mut stack = vec![root_canonical.clone()];

    while let Some(current_dir) = stack.pop() {
        if !current_dir.is_dir() {
            continue;
        }

        let mut entries = match tokio::fs::read_dir(&current_dir).await {
            Ok(e) => e,
            Err(err) => {
                tracing::warn!(
                    event.name = "schema.directory.unreadable",
                    ?current_dir,
                    ?err,
                    "skipping unreadable schema directory"
                );
                continue;
            },
        };

        while let Some(entry) = entries
            .next_entry()
            .await
            .context("failed to iterate directory entries")?
        {
            let path = entry.path();
            let path_canonical = match path.canonicalize() {
                Ok(c) => c,
                Err(_) => continue,
            };

            if !path_canonical.starts_with(&root_canonical) {
                tracing::warn!(
                    event.name = "schema.directory.traversal_blocked",
                    ?path_canonical,
                    ?root_canonical,
                    "blocking path escaping root schema directory"
                );
                continue;
            }

            if path_canonical.is_dir() {
                stack.push(path_canonical);
            } else if path_canonical
                .extension()
                .is_some_and(|ext| ext == "avsc")
            {
                let content = tokio::fs::read_to_string(&path_canonical)
                    .await
                    .with_context(|| {
                        format!(
                            "failed to read schema file: {:?}",
                            path_canonical
                        )
                    })?;
                tracing::debug!(
                    event.name = "schema.file.loaded",
                    path = ?path_canonical,
                    "schema file loaded"
                );
                discovered.push((path_canonical, content));
            }
        }
    }

    Ok(discovered)
}

#[cfg(test)]
mod tests {
    use std::fs::{self, File};
    use std::io::Write;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    static TEST_COUNTER: AtomicUsize = AtomicUsize::new(0);

    struct TestDirGuard {
        path: PathBuf,
    }

    impl TestDirGuard {
        fn new() -> Self {
            let count = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
            let mut raw_path = std::env::temp_dir();
            raw_path.push(format!(
                "galadril_async_schemas_{}_{}",
                std::process::id(),
                count
            ));
            fs::create_dir_all(&raw_path).unwrap();
            let canonical_path = raw_path.canonicalize().unwrap();
            Self {
                path: canonical_path,
            }
        }
    }

    impl Drop for TestDirGuard {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    #[tokio::test]
    async fn discovers_local_schemas_async_and_recursively() {
        let guard = TestDirGuard::new();
        let root = &guard.path;
        let sub_dir = root.join("nested").join("avro");
        fs::create_dir_all(&sub_dir).unwrap();
        let sub_dir_canonical = sub_dir.canonicalize().unwrap();

        let file1_path = root.join("root.avsc");
        let mut f1 = File::create(&file1_path).unwrap();
        f1.write_all(b"root-content").unwrap();

        let file2_path = sub_dir_canonical.join("nested.avsc");
        let mut f2 = File::create(&file2_path).unwrap();
        f2.write_all(b"nested-content").unwrap();

        let file3_path = sub_dir_canonical.join("ignored.txt");
        let mut f3 = File::create(&file3_path).unwrap();
        f3.write_all(b"ignore-me").unwrap();

        let results = discover_local_schemas(root).await.unwrap();

        assert_eq!(results.len(), 2);
        let contents: Vec<String> =
            results.iter().map(|(_, c)| c.clone()).collect();
        assert!(contents.contains(&"root-content".to_string()));
        assert!(contents.contains(&"nested-content".to_string()));
    }
}
