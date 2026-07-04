use std::sync::{Arc, Mutex};

use anyhow::{Result, anyhow};
use mistralrs::tool;
use serde::{Deserialize, Serialize};

/// A section produced by the model via `add_section(title, content)`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Section {
    pub title: String,
    pub content: String,
}

// Ensures that if the agent is handling multiple different requests at the
// same time, each request writes exclusively to its own isolated Vec<Section>.
tokio::task_local! {
    pub static SECTIONS: Arc<Mutex<Vec<Section>>>;
}

/// Tool to add a LaTeX section.
/// Because it is `async`, it stays within the tokio runtime and inherits the
/// `task_local!` state.
#[tool(
    description = "Draft and insert a section into the LaTeX report. IMPORTANT: Before calling this tool, you must think step-by-step in your response, query the database if needed, fact-check your information. The content must be raw LaTeX body (no \\section command, no preamble). Use \\subsection, \\subsubsection, math environments, and TikZ as needed."
)]
pub async fn add_section(
    #[description = "The section title (will be rendered as \\section{TITLE})."]
    title: String,
    #[description = "The LaTeX body content of the section."] content: String,
) -> Result<String> {
    tracing::debug!(title = ?title, "add_section tool invoked by agent");

    let ok = SECTIONS.try_with(|sections_arc| match sections_arc.lock() {
        Ok(mut guard) => {
            guard.push(Section {
                title: title.clone(),
                content,
            });
            Ok(())
        },
        Err(e) => Err(anyhow!("Mutex poisoned: {e:?}")),
    });

    match ok {
        Ok(Ok(_)) => Ok(format!("Successfully drafted section: {}", title)),
        Ok(Err(e)) => Err(anyhow!("Internal error: {}", e)),
        Err(_) => {
            tracing::warn!("task-local sections not found");
            Ok("Failed to save section internally. Ensure the tool is running within the correct scope.".to_string())
        },
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use super::*;

    #[tokio::test]
    async fn test_add_section_success() {
        let sections = Arc::new(Mutex::new(Vec::new()));
        let res = SECTIONS
            .scope(sections.clone(), async {
                add_section(
                    "Introduction".to_string(),
                    "Content body".to_string(),
                )
                .await
            })
            .await;

        assert!(res.is_ok());
        assert_eq!(res.unwrap(), "Successfully drafted section: Introduction");

        let guard = sections.lock().unwrap();
        assert_eq!(guard.len(), 1);
        assert_eq!(guard[0].title, "Introduction");
        assert_eq!(guard[0].content, "Content body");
    }

    #[tokio::test]
    async fn test_add_section_missing_context() {
        let res =
            add_section("Title".to_string(), "Content".to_string()).await;
        assert!(res.is_ok());
        assert!(res.unwrap().contains("Failed to save section internally"));
    }

    #[tokio::test]
    async fn test_add_section_poisoned_mutex() {
        let sections = Arc::new(Mutex::new(Vec::new()));
        let sections_clone = sections.clone();

        let _ = std::thread::spawn(move || {
            let _guard = sections_clone.lock().unwrap();
            panic!("Intentional panic to poison mutex");
        })
        .join();

        let res = SECTIONS
            .scope(sections, async {
                add_section("Title".to_string(), "Content".to_string()).await
            })
            .await;

        assert!(res.is_err());
        assert!(res.unwrap_err().to_string().contains("Internal error"));
    }
}
