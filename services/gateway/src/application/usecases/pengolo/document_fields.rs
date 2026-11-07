//! Published plugin templates select the document fields, never the model.
use std::collections::BTreeMap;

use galadril_document_compiler::tools::{PluginTemplate, plugin_template_id};
use ring::{aead, hmac, rand};

use super::*;

pub type DocumentFields = BTreeMap<String, String>;

fn validate_fields(fields: &DocumentFields) -> Result<()> {
    ensure!(
        fields.len() <= 32 &&
            fields.iter().all(|(key, value)| {
                !key.is_empty() &&
                    key.len() <= 56 &&
                    key.bytes().all(|byte| {
                        byte.is_ascii_lowercase() ||
                            byte.is_ascii_digit() ||
                            byte == b'_'
                    }) &&
                    value.len() <= 256 &&
                    value.chars().all(|character| !character.is_control())
            }),
        "Invalid document fields"
    );
    Ok(())
}

pub fn compiler_binding_key_matches(supplied: &str) -> bool {
    let Ok(expected) = std::env::var("DOCUMENT_COMPILER_BINDING_KEY") else {
        return false;
    };
    if expected.len() != 64 || supplied.len() != 64 {
        return false;
    }
    let key = hmac::Key::new(hmac::HMAC_SHA256, expected.as_bytes());
    let tag = hmac::sign(&key, supplied.as_bytes());
    hmac::verify(&key, expected.as_bytes(), tag.as_ref()).is_ok()
}

fn vault_key() -> Result<aead::LessSafeKey> {
    let value = std::env::var("PENGOLO_DOCUMENT_VAULT_KEY")
        .context("Document vault is unavailable")?;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(value)
        .context("Invalid document vault configuration")?;
    Ok(aead::LessSafeKey::new(
        aead::UnboundKey::new(&aead::AES_256_GCM, &bytes).map_err(|_| {
            anyhow::anyhow!("Invalid document vault configuration")
        })?,
    ))
}

fn vault_aad(actor: &Principal<'_>) -> Result<Vec<u8>> {
    Ok(serde_json::to_vec(&(
        "pengolo-document-profile-v1",
        actor.tenant,
        actor.user,
    ))?)
}

fn seal_profile(
    key: &aead::LessSafeKey,
    aad: &[u8],
    fields: &DocumentFields,
) -> Result<String> {
    use ring::rand::SecureRandom as _;

    let mut nonce = [0; 12];
    rand::SystemRandom::new()
        .fill(&mut nonce)
        .map_err(|_| anyhow::anyhow!("Document vault unavailable"))?;
    let mut data = serde_json::to_vec(fields)?;
    key.seal_in_place_append_tag(
        aead::Nonce::assume_unique_for_key(nonce),
        aead::Aad::from(aad),
        &mut data,
    )
    .map_err(|_| anyhow::anyhow!("Document vault unavailable"))?;
    let mut envelope = Vec::with_capacity(nonce.len() + data.len());
    envelope.extend_from_slice(&nonce);
    envelope.append(&mut data);
    Ok(base64::engine::general_purpose::STANDARD.encode(envelope))
}

fn open_profile(
    key: &aead::LessSafeKey,
    aad: &[u8],
    envelope: &str,
) -> Result<DocumentFields> {
    let mut data =
        base64::engine::general_purpose::STANDARD.decode(envelope)?;
    let (nonce, ciphertext) = data
        .split_at_mut_checked(12)
        .context("Invalid document vault entry")?;
    let text = key
        .open_in_place(
            aead::Nonce::try_assume_unique_for_key(nonce).map_err(|_| {
                anyhow::anyhow!("Invalid document vault entry")
            })?,
            aead::Aad::from(aad),
            ciphertext,
        )
        .map_err(|_| anyhow::anyhow!("Document vault entry unavailable"))?;
    serde_json::from_slice(text).context("Invalid document vault entry")
}

impl PengoloService {
    #[tracing::instrument(skip_all)]
    pub async fn save_document_profile(
        &self,
        actor: &Principal<'_>,
        revision: i64,
        fields: DocumentFields,
    ) -> Result<bool> {
        self.verify(actor).await?;
        validate_fields(&fields)?;
        let existing = self.preference_record(actor).await?;
        ensure!(
            existing.as_ref().map_or(0, |record| record.revision) == revision,
            "Preferences changed; reload before saving"
        );
        let encrypted =
            seal_profile(&vault_key()?, &vault_aad(actor)?, &fields)?;
        let mut record = existing.unwrap_or_else(|| Record {
            id: uuid::Uuid::new_v4().simple().to_string(),
            owner_id: actor.user.into(),
            project_id: None,
            expires_at: None,
            revision: 1,
            updated_at: now(),
            data: json!({"instructions":""}),
        });
        record
            .data
            .as_object_mut()
            .context("Invalid preferences")?
            .insert("documentProfile".into(), json!(encrypted));
        self.store
            .save(
                actor.tenant,
                Collection::Preference,
                &record,
                false,
                (revision > 0).then_some(revision),
            )
            .await?;
        tracing::info!(event.name = "pengolo.document_profile.saved");
        Ok(true)
    }

    async fn document_profile(
        &self,
        actor: &Principal<'_>,
    ) -> Result<DocumentFields> {
        let record = self.preference_record(actor).await?;
        let Some(envelope) = record
            .as_ref()
            .and_then(|record| record.data.get("documentProfile"))
            .and_then(Value::as_str)
        else {
            return Ok(BTreeMap::new());
        };
        open_profile(&vault_key()?, &vault_aad(actor)?, envelope)
    }

    async fn document_case_template(
        &self,
        actor: &Principal<'_>,
        project: &Record,
        template_id: &str,
    ) -> Result<PluginTemplate> {
        let context = execution_context(project)?
            .context("Document case requires a registered project")?;
        let catalog = self.agent.capabilities().await?;
        let context = &catalog.resolve_execution_context(&context)?.id;
        let template =
            self.agent
                .document_templates()
                .await?
                .into_iter()
                .find(|entry| {
                    plugin_template_id(
                        actor.tenant,
                        &entry.plugin_id,
                        &entry.key,
                    ) == template_id
                })
                .context("Document case template unavailable")?;
        ensure!(
            template.execution_context.as_ref() == Some(context) &&
                catalog.capabilities.iter().any(|capability| {
                    capability.plugin_id.as_deref() ==
                        Some(template.plugin_id.as_str()) &&
                        capability
                            .execution_profile
                            .contexts
                            .iter()
                            .any(|candidate| &candidate.id == context)
                }),
            "Document case template is outside the project context"
        );
        ensure!(
            self.auth
                .is_authorized(
                    actor.user,
                    actor.tenant,
                    Permission::Use,
                    "document_plugin",
                    &template.plugin_id,
                    Some(actor.context)
                )
                .await?,
            "Document plugin is unavailable"
        );
        Ok(template)
    }

    pub(super) async fn document_case(
        &self,
        actor: &Principal<'_>,
        project_id: &str,
        template_id: &str,
        update: Option<DocumentFields>,
    ) -> Result<Value> {
        let mut project = self.project(actor, project_id).await?;
        let template = self
            .document_case_template(actor, &project, template_id)
            .await?;
        let mut fields = project_fields(&project, template_id)?;
        if let Some(update) = update {
            ensure!(
                project.owner_id == actor.user,
                "Only the project owner may update document fields"
            );
            validate_fields(&update)?;
            ensure!(
                update.keys().all(|key| template
                    .template
                    .variables
                    .contains_key(&format!("project_{key}"))),
                "Unknown document case field"
            );
            fields.extend(update);
            let data =
                project.data.as_object_mut().context("Invalid project")?;
            let cases = data
                .entry("documentFields")
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .context("Invalid document case")?;
            cases.insert(template_id.into(), serde_json::to_value(&fields)?);
            self.store
                .save(
                    actor.tenant,
                    Collection::Project,
                    &project,
                    false,
                    Some(project.revision),
                )
                .await?;
            tracing::info!(event.name = "pengolo.document_case.saved");
        }
        let profile = self.document_profile(actor).await?;
        let missing: Vec<_> = template
            .template
            .variables
            .iter()
            .filter(|(_, variable)| variable.required)
            .filter_map(|(name, _)| name.strip_prefix("project_"))
            .filter(|name| {
                fields
                    .get(*name)
                    .is_none_or(|value| value.trim().is_empty())
            })
            .collect();
        let profile_ready = template
            .template
            .variables
            .iter()
            .filter(|(_, variable)| variable.required)
            .filter_map(|(name, _)| name.strip_prefix("profile_"))
            .all(|name| {
                profile
                    .get(name)
                    .is_some_and(|value| !value.trim().is_empty())
            });
        Ok(
            json!({"missing_project_fields":missing,"profile_ready":profile_ready,"can_update":project.owner_id == actor.user}),
        )
    }

    pub(super) async fn bind_document(
        &self,
        actor: &Principal<'_>,
        project_id: &str,
        template_id: &str,
    ) -> Result<BTreeMap<String, Value>> {
        let project = self.project(actor, project_id).await?;
        let template = self
            .document_case_template(actor, &project, template_id)
            .await?;
        let fields = project_fields(&project, template_id)?;
        let profile = self.document_profile(actor).await?;
        let mut bindings = BTreeMap::new();
        for (name, variable) in template.template.variables {
            let value = if let Some(field) = name.strip_prefix("project_") {
                fields.get(field)
            } else if let Some(field) = name.strip_prefix("profile_") {
                profile.get(field)
            } else {
                continue;
            };
            match value.filter(|value| !value.trim().is_empty()) {
                Some(value) => {
                    bindings.insert(name, json!(value));
                },
                None => {
                    ensure!(!variable.required, "Document field is missing")
                },
            }
        }
        Ok(bindings)
    }
}

fn project_fields(
    project: &Record,
    template_id: &str,
) -> Result<DocumentFields> {
    project
        .data
        .get("documentFields")
        .and_then(|cases| cases.get(template_id))
        .map(|value| serde_json::from_value(value.clone()))
        .transpose()
        .map(|fields| fields.unwrap_or_default())
        .context("Invalid document case")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_ciphertext_is_bound_to_the_authenticated_owner() -> Result<()> {
        let key = aead::LessSafeKey::new(
            aead::UnboundKey::new(&aead::AES_256_GCM, &[4; 32])
                .map_err(|_| anyhow::anyhow!("Invalid fixture key"))?,
        );
        let profile =
            BTreeMap::from([("first_name".into(), "Test fixture".into())]);
        let sealed = seal_profile(&key, b"tenant/actor", &profile)?;
        assert_ne!(sealed, seal_profile(&key, b"tenant/actor", &profile)?);
        assert_eq!(open_profile(&key, b"tenant/actor", &sealed)?, profile);
        assert!(open_profile(&key, b"tenant/other", &sealed).is_err());
        assert!(open_profile(&key, b"other/actor", &sealed).is_err());
        Ok(())
    }
}
