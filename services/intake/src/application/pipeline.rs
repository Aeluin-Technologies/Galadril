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
    let mut discovered = Vec::new();
    let mut stack = vec![root_dir.as_ref().to_path_buf()];

    while let Some(current_dir) = stack.pop() {
        if !current_dir.is_dir() {
            continue;
        }

        let mut entries = match tokio::fs::read_dir(&current_dir).await {
            Ok(e) => e,
            Err(err) => {
                tracing::warn!(
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
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|ext| ext == "avsc") {
                let content =
                    tokio::fs::read_to_string(&path).await.with_context(
                        || format!("failed to read schema file: {:?}", path),
                    )?;
                tracing::debug!(?path, "schema file loaded");
                discovered.push((path, content));
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
            let mut path = std::env::temp_dir();
            path.push(format!(
                "galadril_async_schemas_{}_{}",
                std::process::id(),
                count
            ));
            fs::create_dir_all(&path).unwrap();
            Self { path }
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
        let sub_dir = guard.path.join("nested").join("avro");
        fs::create_dir_all(&sub_dir).unwrap();

        let mut f1 = File::create(guard.path.join("root.avsc")).unwrap();
        f1.write_all(b"root-content").unwrap();

        let mut f2 = File::create(sub_dir.join("nested.avsc")).unwrap();
        f2.write_all(b"nested-content").unwrap();

        let mut f3 = File::create(sub_dir.join("ignored.txt")).unwrap();
        f3.write_all(b"ignore-me").unwrap();

        let results = discover_local_schemas(&guard.path).await.unwrap();

        assert_eq!(results.len(), 2);
        let contents: Vec<String> =
            results.iter().map(|(_, c)| c.clone()).collect();
        assert!(contents.contains(&"root-content".to_string()));
        assert!(contents.contains(&"nested-content".to_string()));
    }
}
