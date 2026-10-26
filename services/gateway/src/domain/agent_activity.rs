//! Public agent activity and human answers carry no execution authority.

use std::collections::HashSet;

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;

fn identifier(value: &str) -> bool {
    !value.is_empty() &&
        value.len() <= 128 &&
        value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-')
        })
}
fn bounded(value: &str, maximum: usize, empty: bool) -> bool {
    (empty || !value.trim().is_empty()) && value.chars().count() <= maximum
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Tool,
    Thinking,
    Writing,
    Agent,
    Waiting,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WarningSource {
    Search,
    Graph,
    Causal,
    Library,
    ReadFile,
    DocumentSearch,
    SearchEvidence,
    Attachment,
    Web,
    RunPython,
    Tool,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PlanOperation {
    Upsert,
    Delete,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Pending,
    InProgress,
    Completed,
    Cancelled,
    Blocked,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PlanTask {
    pub id: String,
    pub content: String,
    pub status: TaskStatus,
    #[serde(default)]
    pub active_form: String,
    pub parent_id: Option<String>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QuestionOption {
    pub label: String,
    pub description: Option<String>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Question {
    pub header: String,
    pub question: String,
    pub options: Vec<QuestionOption>,
    #[serde(default)]
    pub multi_select: bool,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum AgentActivity {
    EmailDraft {
        id: String,
        to: Vec<String>,
        cc: Vec<String>,
        bcc: Vec<String>,
        subject: String,
        body: String,
    },
    Billing {
        provider: String,
        reason: String,
    },
    Python {
        id: String,
        channel: String,
        data: String,
    },
    Warning {
        source: WarningSource,
    },
    Phase {
        phase: Phase,
        agent_id: Option<String>,
    },
    Plan {
        operation: PlanOperation,
        agent_id: Option<String>,
        task: PlanTask,
    },
    Question {
        id: String,
        questions: Vec<Question>,
    },
    QuestionResolved {
        id: String,
        cancelled: bool,
    },
}
impl AgentActivity {
    /// Screens progress before storage so private reasoning cannot enter
    /// replay.
    pub fn parse(content: &str) -> Result<Self> {
        ensure!(content.len() <= 65536, "Activity exceeds limit");
        let activity: Self = serde_json::from_str(content)
            .map_err(|_| anyhow::anyhow!("Invalid agent activity"))?;
        match &activity {
            Self::EmailDraft {
                id,
                to,
                cc,
                bcc,
                subject,
                body,
            } => {
                ensure!(
                    identifier(id) &&
                        !to.is_empty() &&
                        to.len() <= 20 &&
                        cc.len() <= 20 &&
                        bcc.len() <= 20,
                    "Invalid email proposal"
                );
                ensure!(
                    bounded(subject, 998, false) &&
                        bounded(body, 16384, false) &&
                        !subject.chars().any(char::is_control),
                    "Invalid email content"
                );
                ensure!(
                    to.iter().chain(cc).chain(bcc).all(|address| address
                        .len() <=
                        320 &&
                        address.contains('@') &&
                        !address.chars().any(char::is_control)),
                    "Invalid email recipients"
                );
            },
            Self::Billing { provider, reason } => ensure!(
                matches!(provider.as_str(), "chatgpt" | "api") &&
                    matches!(
                        reason.as_str(),
                        "connected" |
                            "not_connected" |
                            "quota" |
                            "model_unavailable"
                    ),
                "Invalid billing labels"
            ),
            Self::Python { id, channel, data } => {
                ensure!(identifier(id), "Invalid execution ID");
                ensure!(
                    match channel.as_str() {
                        "code" | "output" => data.chars().count() <= 256,
                        "status" => matches!(
                            data.as_str(),
                            "running" | "completed" | "failed"
                        ),
                        "image" =>
                            data.len() <= 2048 &&
                                data.len() -
                                    data.trim_end_matches('=').len() <=
                                    2 &&
                                data.trim_end_matches('=')
                                    .bytes()
                                    .all(|byte| byte
                                        .is_ascii_alphanumeric() ||
                                        matches!(byte, b'+' | b'/')),
                        _ => false,
                    },
                    "Invalid Python activity fragment"
                );
            },
            Self::Warning { .. } => {},
            Self::Phase { agent_id, .. } => ensure!(
                agent_id.as_deref().is_none_or(identifier),
                "Invalid agent ID"
            ),
            Self::Plan { agent_id, task, .. } => {
                ensure!(
                    agent_id.as_deref().is_none_or(identifier) &&
                        identifier(&task.id) &&
                        task.parent_id.as_deref().is_none_or(identifier),
                    "Invalid plan identifier"
                );
                ensure!(
                    bounded(&task.content, 2048, false) &&
                        bounded(&task.active_form, 2048, true),
                    "Invalid plan task"
                );
            },
            Self::Question { id, questions } => {
                ensure!(
                    identifier(id) &&
                        !questions.is_empty() &&
                        questions.len() <= 10,
                    "Invalid question set"
                );
                let mut headers = HashSet::with_capacity(questions.len());
                for question in questions {
                    ensure!(
                        bounded(&question.header, 25, false) &&
                            bounded(&question.question, 500, false) &&
                            headers.insert(question.header.as_str()),
                        "Invalid question"
                    );
                    ensure!(
                        (2..=6).contains(&question.options.len()),
                        "Invalid question options"
                    );
                    let mut labels =
                        HashSet::with_capacity(question.options.len());
                    for option in &question.options {
                        ensure!(
                            bounded(&option.label, 50, false) &&
                                labels.insert(option.label.as_str()) &&
                                option.description.as_deref().is_none_or(
                                    |description| bounded(
                                        description,
                                        200,
                                        true
                                    )
                                ),
                            "Invalid question option"
                        );
                    }
                }
            },
            Self::QuestionResolved { id, .. } => {
                ensure!(identifier(id), "Invalid question ID")
            },
        }
        Ok(activity)
    }
}

#[cfg(test)]
#[test]
fn billing_events_accept_only_public_closed_labels() -> Result<()> {
    AgentActivity::parse(
        r#"{"type":"billing","provider":"api","reason":"quota"}"#,
    )?;
    assert!(AgentActivity::parse(r#"{"type":"billing","provider":"api","reason":"quota","token":"secret"}"#).is_err());
    assert!(
        AgentActivity::parse(
            r#"{"type":"billing","provider":"api","reason":"arbitrary"}"#
        )
        .is_err()
    );
    Ok(())
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct AnswerValue {
    pub header: String,
    #[serde(default)]
    pub selected: Vec<String>,
    pub custom_answer: Option<String>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct AnswerInput {
    pub id: String,
    pub question_id: String,
    #[serde(default)]
    pub answers: Vec<AnswerValue>,
    #[serde(default)]
    pub cancelled: bool,
    #[serde(default)]
    pub temporary: bool,
}
impl AnswerInput {
    /// Gateway validates shape; Scribe validates labels against the live
    /// question.
    pub fn parse(value: Value) -> Result<Self> {
        ensure!(
            serde_json::to_vec(&value)?.len() <= 65536,
            "Answer exceeds limit"
        );
        let input: Self = serde_json::from_value(value)
            .map_err(|_| anyhow::anyhow!("Invalid human answer"))?;
        ensure!(
            input.id.len() == 32 &&
                input.id.bytes().all(|byte| byte.is_ascii_hexdigit()) &&
                identifier(&input.question_id),
            "Invalid answer scope"
        );
        ensure!(
            input.answers.len() <= 10 &&
                if input.cancelled {
                    input.answers.is_empty()
                } else {
                    !input.answers.is_empty()
                },
            "Invalid answer set"
        );
        let mut headers = HashSet::with_capacity(input.answers.len());
        for answer in &input.answers {
            ensure!(
                bounded(&answer.header, 25, false) &&
                    headers.insert(answer.header.as_str()),
                "Invalid answer header"
            );
            ensure!(
                answer.selected.len() <= 6 &&
                    answer
                        .custom_answer
                        .as_ref()
                        .is_none_or(|_| answer.selected.is_empty()),
                "Ambiguous answer"
            );
            let mut selected = HashSet::with_capacity(answer.selected.len());
            for label in &answer.selected {
                ensure!(
                    bounded(label, 50, false) &&
                        selected.insert(label.as_str()),
                    "Invalid answer selection"
                );
            }
            if let Some(custom) = &answer.custom_answer {
                ensure!(
                    bounded(custom, 4096, false) &&
                        !custom
                            .chars()
                            .any(|character| character.is_control() &&
                                character != '\n'),
                    "Invalid custom answer"
                );
            } else {
                ensure!(!answer.selected.is_empty(), "Missing answer");
            }
        }
        Ok(input)
    }

    pub fn scribe_payload(&self) -> Value {
        serde_json::json!({"question_id":self.question_id,"answers":self.answers.iter().map(|answer| serde_json::json!({"header":answer.header,"selected":answer.selected,"custom_answer":answer.custom_answer})).collect::<Vec<_>>(),"cancelled":self.cancelled})
    }
}

#[cfg(test)]
mod tests {
    use anyhow::{Result, ensure};
    use serde_json::json;

    use super::*;

    #[test]
    fn email_drafts_have_no_sender_or_execution_authority() -> Result<()> {
        let value = serde_json::json!({"type":"email_draft","id":"draft_1","to":["to@example.org"],"cc":[],"bcc":[],"subject":"Hello","body":"A draft"});
        AgentActivity::parse(&value.to_string())?;
        let mut injected = value;
        if let Some(fields) = injected.as_object_mut() {
            fields.insert(
                "account".into(),
                serde_json::json!("another-account"),
            );
        }
        ensure!(
            AgentActivity::parse(&injected.to_string()).is_err(),
            "Sender authority must remain in the interface"
        );
        Ok(())
    }

    #[test]
    fn agent_activity_accepts_bounded_plans_and_questions() -> Result<()> {
        for payload in [
            json!({"type":"phase","phase":"thinking","agent_id":null}),
            json!({"type":"plan","operation":"upsert","agent_id":"news","task":{"id":"research","content":"Check sources","status":"in_progress","active_form":"Checking sources","parent_id":null}}),
            json!({"type":"question","id":"q1","questions":[{"header":"Sources","question":"Which sources?","options":[{"label":"Official","description":null},{"label":"All public","description":null}],"multi_select":false}]}),
            json!({"type":"question_resolved","id":"q1","cancelled":false}),
            json!({"type":"warning","source":"read_file"}),
            json!({"type":"warning","source":"search_evidence"}),
            json!({"type":"warning","source":"document_search"}),
            json!({"type":"python","id":"run1","channel":"code","data":"print(42)"}),
            json!({"type":"python","id":"run1","channel":"status","data":"completed"}),
        ] {
            AgentActivity::parse(&serde_json::to_string(&payload)?)?;
        }
        for invalid in [
            json!({"type":"phase","phase":"thinking","content":"hidden reasoning"}),
            json!({"type":"question","id":"q1","questions":[]}),
            json!({"type":"question","id":"q1","token":"private","questions":[]}),
            json!({"type":"plan","operation":"upsert","task":{"id":"t","content":"x","status":"invalid"}}),
            json!({"type":"question_resolved","id":"invalid/id","cancelled":false}),
            json!({"type":"warning","source":"arbitrary_private_value"}),
            json!({"type":"warning","source":"read_file","details":"private"}),
            json!({"type":"python","id":"run1","channel":"image","data":"<script>"}),
            json!({"type":"python","id":"run1","channel":"code","data":"x".repeat(257)}),
        ] {
            ensure!(
                AgentActivity::parse(&serde_json::to_string(&invalid)?)
                    .is_err()
            );
        }
        Ok(())
    }

    #[test]
    fn user_answers_are_bounded_without_credential_fields() -> Result<()> {
        let valid = json!({"id":"a".repeat(32),"questionId":"q1","answers":[{"header":"Sources","selected":["Official"]}],"temporary":false});
        AnswerInput::parse(valid.clone())?;
        for update in [
            json!({"capability":"secret"}),
            json!({"answers":[{"header":"Sources","selected":["Official","Official"]}]}),
            json!({"cancelled":true}),
            json!({"answers":[{"header":"Sources","selected":["Official"],"customAnswer":"Different"}]}),
            json!({"questionId":"x".repeat(129)}),
        ] {
            let mut invalid = valid.clone();
            if let (Some(target), Some(fields)) =
                (invalid.as_object_mut(), update.as_object())
            {
                target.extend(
                    fields
                        .iter()
                        .map(|(key, value)| (key.clone(), value.clone())),
                );
            }
            ensure!(AnswerInput::parse(invalid).is_err());
        }
        Ok(())
    }
}
