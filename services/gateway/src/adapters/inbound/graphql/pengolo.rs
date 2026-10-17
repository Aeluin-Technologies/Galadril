//! Typed workspace projections keep selections and pagination at the API
//! boundary.

use juniper::{
    FieldResult, GraphQLEnum, GraphQLInputObject, GraphQLObject,
    graphql_object, graphql_scalar,
};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(transparent)]
#[graphql_scalar(name = "Long", with = long_scalar)]
pub struct Long(pub i64);

mod long_scalar {
    use juniper::{ParseScalarResult, ScalarToken, ScalarValue};

    use super::Long;

    pub fn to_output<S: ScalarValue>(value: &Long) -> S {
        S::from(value.0 as f64)
    }

    pub fn from_input<S: ScalarValue>(
        value: &juniper::Scalar<S>,
    ) -> Result<Long, String> {
        let number = value
            .try_to_int()
            .map(f64::from)
            .or_else(|| value.try_to_float())
            .ok_or("Expected an integer")?;
        if !number.is_finite() ||
            number.fract() != 0.0 ||
            number.abs() > 9_007_199_254_740_991.0
        {
            return Err("Integer exceeds the interoperable range".into());
        }
        Ok(Long(number as i64))
    }

    pub fn parse_token<S: ScalarValue>(
        value: ScalarToken<'_>,
    ) -> ParseScalarResult<S> {
        match value {
            ScalarToken::Int(_) | ScalarToken::Float(_) => {
                <f64 as juniper::ParseScalarValue<S>>::from_str(value)
            },
            _ => <String as juniper::ParseScalarValue<S>>::from_str(value),
        }
    }
}

macro_rules! choice {
    ($name:ident, $($variant:ident => $wire:literal),+ $(,)?) => {
        #[derive(Debug, Clone, Copy, Serialize, Deserialize, GraphQLEnum)]
        pub enum $name { $(#[serde(rename = $wire)] #[graphql(name = $wire)] $variant),+ }
    };
}
choice!(PengoloGraphKind, Entity => "entity", Event => "event", Relation => "relation");
choice!(PengoloRole, User => "user", Assistant => "assistant");
choice!(PengoloStatus, Pending => "pending", Running => "running", Completed => "completed", Failed => "failed", Cancelled => "cancelled");
choice!(PengoloEventKind, Content => "content", Tool => "tool", Completed => "completed", Failed => "failed", Cancelled => "cancelled");
choice!(PengoloFileStatus, Uploading => "uploading", Ready => "ready", Deleted => "deleted");
choice!(PengoloContextMode, Personal => "personal", Project => "project");
choice!(PengoloPartKind, Markdown => "markdown", Reference => "reference", Artifact => "artifact");

#[derive(Debug, Clone, Deserialize, GraphQLObject)]
#[graphql(name = "PengoloGraphContext")]
pub struct GraphContext {
    pub kind: PengoloGraphKind,
    pub id: String,
}

#[derive(Debug, Clone, Serialize, GraphQLInputObject)]
#[graphql(name = "PengoloGraphContextInput")]
pub struct GraphContextInput {
    pub kind: PengoloGraphKind,
    pub id: String,
}

#[derive(Debug, Clone, Deserialize, GraphQLObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloGenerationContext")]
pub struct GenerationContext {
    pub graph_context: Option<GraphContext>,
    pub model_alias: String,
    pub web_search: bool,
}

#[derive(Debug, Clone, Deserialize, GraphQLObject)]
#[graphql(name = "PengoloEvidence")]
pub struct Evidence {
    pub resource_type: String,
    pub resource_id: String,
    pub entity_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, GraphQLObject)]
#[graphql(name = "PengoloTaskCheck")]
pub struct TaskCheck {
    pub index: i32,
    pub checked: bool,
}

#[derive(Debug, Clone, Serialize, GraphQLInputObject)]
#[graphql(name = "PengoloTaskCheckInput")]
pub struct TaskCheckInput {
    pub index: i32,
    pub checked: bool,
}

#[derive(Debug, Clone, Deserialize, GraphQLObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloResponsePart")]
pub struct ResponsePart {
    pub kind: PengoloPartKind,
    pub text: Option<String>,
    pub resource_type: Option<String>,
    pub resource_id: Option<String>,
    pub label: Option<String>,
    pub artifact_type: Option<String>,
    pub id: Option<String>,
    pub title: Option<String>,
}

#[derive(Debug, Clone, Deserialize, GraphQLObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloMessage")]
pub struct Message {
    pub id: String,
    pub role: PengoloRole,
    pub text: String,
    #[serde(default)]
    pub file_ids: Vec<String>,
    pub generation_id: Option<String>,
    pub status: Option<PengoloStatus>,
    #[serde(default)]
    pub evidence: Vec<Evidence>,
    #[serde(default)]
    pub parts: Vec<ResponsePart>,
    #[serde(default)]
    pub task_checks: Vec<TaskCheck>,
}

#[derive(GraphQLObject)]
#[graphql(name = "PengoloMessagePage")]
pub struct MessagePage<'a> {
    pub items: Vec<&'a Message>,
    pub after: Option<&'a str>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChatData {
    pub title: String,
    pub pinned: bool,
    pub active_generation: Option<String>,
    pub generation_context: Option<GenerationContext>,
    #[serde(default)]
    pub messages: Vec<Message>,
}

#[graphql_object(name = "PengoloChatData")]
impl ChatData {
    fn title(&self) -> &str {
        &self.title
    }

    fn pinned(&self) -> bool {
        self.pinned
    }

    fn active_generation(&self) -> Option<&str> {
        self.active_generation.as_deref()
    }

    fn generation_context(&self) -> Option<&GenerationContext> {
        self.generation_context.as_ref()
    }

    fn messages(
        &self,
        before: Option<String>,
        limit: Option<i32>,
    ) -> FieldResult<MessagePage<'_>> {
        let limit = usize::try_from(limit.unwrap_or(10).clamp(1, 100))
            .map_err(|_| "Invalid page size")?;
        let end = match before {
            Some(id) => self
                .messages
                .iter()
                .position(|message| message.id == id)
                .ok_or("Message cursor unavailable")?,
            None => self.messages.len(),
        };
        let start = end.saturating_sub(limit);
        let items = self
            .messages
            .get(start..end)
            .ok_or("Message page unavailable")?
            .iter()
            .collect();
        Ok(MessagePage {
            items,
            after: if start > 0 {
                self.messages.get(start).map(|message| message.id.as_str())
            } else {
                None
            },
        })
    }
}

#[derive(Debug, Clone, Deserialize, GraphQLObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloProjectData")]
pub struct ProjectData {
    pub name: String,
    pub icon: String,
    pub color: String,
    pub shared: bool,
    pub roles: Vec<String>,
    pub context_mode: PengoloContextMode,
}

#[derive(Debug, Clone, Deserialize, GraphQLObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloFileData")]
pub struct FileData {
    pub name: String,
    pub mime: String,
    pub size: Long,
    pub status: PengoloFileStatus,
    pub original_name: Option<String>,
    pub temporary_session: Option<String>,
}

#[derive(Debug, Clone, Deserialize, GraphQLObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloGenerationData")]
pub struct GenerationData {
    pub status: PengoloStatus,
    pub chat_id: String,
    pub sequence: i32,
}

macro_rules! record {
    ($name:ident, $data:ty, $wire:literal) => {
        #[derive(Debug, Clone, Deserialize, GraphQLObject)]
        #[serde(rename_all = "camelCase")]
        #[graphql(name = $wire)]
        pub struct $name {
            pub id: String,
            pub owner_id: String,
            pub project_id: Option<String>,
            pub expires_at: Option<Long>,
            pub revision: Long,
            pub updated_at: Long,
            pub data: $data,
        }
    };
}
record!(Chat, ChatData, "PengoloChat");
record!(Project, ProjectData, "PengoloProject");
record!(File, FileData, "PengoloFile");
record!(Generation, GenerationData, "PengoloGeneration");

#[derive(Deserialize, GraphQLObject)]
#[graphql(name = "PengoloChatPage")]
pub struct ChatPage {
    pub items: Vec<Chat>,
    pub after: Option<String>,
}

#[derive(Deserialize, GraphQLObject)]
#[graphql(name = "PengoloFilePage")]
pub struct FilePage {
    pub items: Vec<File>,
    pub after: Option<String>,
}

#[derive(Deserialize)]
pub struct SendResult {
    pub chat: Chat,
    pub generation: Generation,
}

#[graphql_object(name = "PengoloSendResult")]
impl SendResult {
    fn chat(&self) -> &Chat {
        &self.chat
    }

    fn generation(&self) -> &Generation {
        &self.generation
    }

    fn user_message(&self) -> Option<&Message> {
        self.chat.data.messages.iter().find(|message| {
            matches!(message.role, PengoloRole::User) &&
                message.generation_id.as_deref() ==
                    Some(self.generation.id.as_str())
        })
    }
}

#[derive(Debug, Deserialize, GraphQLObject)]
#[graphql(name = "PengoloOutputEvent")]
pub struct OutputEvent {
    pub sequence: i32,
    pub kind: PengoloEventKind,
    pub content: String,
}

#[derive(Deserialize, GraphQLObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloReplay")]
pub struct Replay {
    pub events: Vec<OutputEvent>,
    pub status: PengoloStatus,
    pub generation_id: String,
    pub message: Option<Message>,
    pub chat_revision: Option<Long>,
    pub chat_updated_at: Option<Long>,
}

#[derive(Deserialize, GraphQLObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloIdentity")]
pub struct Identity {
    pub version: String,
    pub web_search_available: bool,
    #[serde(default)]
    pub administrator: bool,
}

#[derive(Deserialize, GraphQLObject)]
#[graphql(name = "PengoloDownload")]
pub struct Download {
    pub url: String,
}

#[derive(Deserialize, GraphQLObject)]
#[graphql(name = "PengoloUploadHeader")]
pub struct UploadHeader {
    pub name: String,
    pub value: String,
}

fn upload_headers<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Vec<UploadHeader>, D::Error> {
    let headers = std::collections::BTreeMap::<String, String>::deserialize(
        deserializer,
    )?;
    Ok(headers
        .into_iter()
        .map(|(name, value)| UploadHeader { name, value })
        .collect())
}

#[derive(Deserialize, GraphQLObject)]
#[graphql(name = "PengoloUpload")]
pub struct Upload {
    pub url: String,
    pub file: File,
    #[serde(deserialize_with = "upload_headers")]
    pub headers: Vec<UploadHeader>,
}

#[derive(Serialize, GraphQLInputObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloSendInput")]
pub struct SendInput {
    pub idempotency_key: String,
    pub chat_id: Option<String>,
    pub project_id: Option<String>,
    pub prompt: String,
    pub model_alias: String,
    #[graphql(default = Some(Vec::new()))]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file_ids: Option<Vec<String>>,
    pub graph_context: Option<GraphContextInput>,
    pub web_query: Option<String>,
    #[graphql(default = Some(false))]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub web_search: Option<bool>,
    #[graphql(default = Some(false))]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temporary: Option<bool>,
    #[graphql(default = Some(false))]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub with_library: Option<bool>,
}

#[derive(Serialize, GraphQLInputObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloChatInput")]
pub struct ChatInput {
    pub id: String,
    pub revision: Long,
    pub title: Option<String>,
    pub pinned: Option<bool>,
    pub project_id: Option<String>,
    #[graphql(default = Some(false))]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub move_project: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub task_checks: Option<Vec<TaskCheckInput>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temporary: Option<bool>,
}

#[derive(Serialize, GraphQLInputObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloCancelInput")]
pub struct CancelInput {
    pub id: String,
    #[graphql(default = Some(false))]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temporary: Option<bool>,
}

#[derive(Serialize, GraphQLInputObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloTemporaryInput")]
pub struct TemporaryInput {
    pub id: String,
}

#[derive(Serialize, GraphQLInputObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloFileInput")]
pub struct FileInput {
    pub id: String,
    pub temporary_session: Option<String>,
}

#[derive(Serialize, GraphQLInputObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloProjectInput")]
pub struct ProjectInput {
    pub id: Option<String>,
    pub revision: Option<Long>,
    pub name: String,
    pub icon: String,
    pub color: String,
    #[graphql(default = Some(false))]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub shared: Option<bool>,
    #[graphql(default = Some(Vec::new()))]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub roles: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_mode: Option<PengoloContextMode>,
}

#[derive(Serialize, GraphQLInputObject)]
#[serde(rename_all = "camelCase")]
#[graphql(name = "PengoloUploadInput")]
pub struct UploadInput {
    pub name: String,
    pub mime: String,
    pub size: Long,
    pub checksum: String,
    pub project_id: Option<String>,
    pub temporary_session: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    struct InputContractQuery;

    #[juniper::graphql_object]
    impl InputContractQuery {
        fn send(input: SendInput) -> bool {
            serde_json::to_value(input).is_ok_and(|value| {
                crate::domain::pengolo::SendInput::parse(value).is_ok_and(
                    |input| {
                        input.file_ids.is_empty() &&
                            !input.temporary &&
                            !input.with_library
                    },
                )
            })
        }
    }

    #[test]
    fn omitted_send_options_have_valid_typed_defaults() -> anyhow::Result<()> {
        let schema = juniper::RootNode::new(
            InputContractQuery,
            juniper::EmptyMutation::<()>::new(),
            juniper::EmptySubscription::<()>::new(),
        );
        let variables = serde_json::from_value(serde_json::json!({"input":{
            "idempotencyKey":"12345678-1234-1234-1234-123456789abc",
            "prompt":"Question", "modelAlias":"fixture"
        }}))?;
        let (result, errors) = juniper::execute_sync(
            "query($input:PengoloSendInput!) { send(input:$input) }",
            None,
            &schema,
            &variables,
            &(),
        )?;
        anyhow::ensure!(errors.is_empty(), "Typed input resolution failed");
        assert_eq!(
            serde_json::to_value(result)?,
            serde_json::json!({"send":true})
        );
        Ok(())
    }

    #[test]
    fn default_history_page_contains_only_ten_recent_messages()
    -> anyhow::Result<()> {
        let data: ChatData = serde_json::from_value(
            serde_json::json!({"title":"History","pinned":false,"messages":(0..30).map(|index| serde_json::json!({"id":index.to_string(),"role":"user","text":"message","fileIds":["attachment"]})).collect::<Vec<_>>()}),
        )?;
        let page = data
            .messages(None, None)
            .map_err(|_| anyhow::anyhow!("Page failed"))?;
        assert_eq!(page.items.len(), 10);
        assert_eq!(page.after, Some("20"));
        Ok(())
    }

    #[test]
    fn pages_preserve_attachments_and_never_return_the_whole_history()
    -> anyhow::Result<()> {
        let data: ChatData = serde_json::from_value(
            serde_json::json!({"title":"History","pinned":false,"activeGeneration":null,"messages":[{"id":"one","role":"user","text":"Old","fileIds":["document"]},{"id":"two","role":"assistant","text":"Middle"},{"id":"three","role":"user","text":"Recent"}]}),
        )?;
        let recent = data
            .messages(None, Some(2))
            .map_err(|_| anyhow::anyhow!("Page failed"))?;
        assert_eq!(
            recent
                .items
                .iter()
                .map(|message| message.id.as_str())
                .collect::<Vec<_>>(),
            ["two", "three"]
        );
        assert_eq!(recent.after, Some("two"));
        let older = data
            .messages(recent.after.map(str::to_owned), Some(2))
            .map_err(|_| anyhow::anyhow!("Page failed"))?;
        let message = older
            .items
            .first()
            .ok_or_else(|| anyhow::anyhow!("Missing older message"))?;
        assert_eq!(message.file_ids, ["document"]);
        assert_eq!(older.after, None);
        assert!(data.messages(Some("missing".into()), Some(2)).is_err());
        Ok(())
    }
}
