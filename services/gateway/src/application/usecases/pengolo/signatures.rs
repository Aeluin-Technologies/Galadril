//! Personal handwritten marks.
use ring::rand::SecureRandom as _;
use ring::{aead, rand};

use super::*;
use crate::application::ports::pengolo_store::SignatureMetadata;

const MAX_TRACE_BYTES: usize = 16_384;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SigningVault<'a> {
    version: u8,
    certificate: &'a str,
    #[serde(borrow)]
    password: SigningWrap<'a>,
    #[serde(borrow)]
    passkey: Option<SigningPasskeyWrap<'a>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SigningWrap<'a> {
    salt: &'a str,
    nonce: &'a str,
    ciphertext: &'a str,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SigningPasskeyWrap<'a> {
    credential: &'a str,
    salt: &'a str,
    nonce: &'a str,
    ciphertext: &'a str,
}

fn valid_b64(value: &str, min: usize, max: usize) -> bool {
    (min..=max).contains(&value.len()) &&
        value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_')
        })
}

fn valid_wrap(salt: &str, nonce: &str, ciphertext: &str) -> bool {
    valid_b64(salt, 43, 43) &&
        valid_b64(nonce, 16, 16) &&
        valid_b64(ciphertext, 1000, 4096)
}

fn validate_signing_vault(envelope: &str) -> Result<()> {
    ensure!(envelope.len() <= 10_000, "Invalid signing vault");
    let vault: SigningVault<'_> = serde_json::from_str(envelope)
        .map_err(|_| anyhow::anyhow!("Invalid signing vault"))?;
    ensure!(
        vault.version == 1 &&
            valid_b64(vault.certificate, 128, 2048) &&
            valid_wrap(
                vault.password.salt,
                vault.password.nonce,
                vault.password.ciphertext
            ) &&
            vault.passkey.is_none_or(|slot| {
                valid_b64(slot.credential, 16, 1400) &&
                    valid_wrap(slot.salt, slot.nonce, slot.ciphertext)
            }),
        "Invalid signing vault"
    );
    Ok(())
}

fn validate_trace(trace: &str) -> Result<()> {
    ensure!(
        trace.len() <= MAX_TRACE_BYTES,
        "Signature trace is too large"
    );
    if trace.trim_start().starts_with('{') {
        return validate_signature_image(trace);
    }
    let strokes: Vec<Vec<[u16; 2]>> = serde_json::from_str(trace)
        .map_err(|_| anyhow::anyhow!("Invalid signature trace"))?;
    ensure!(
        !strokes.is_empty() &&
            strokes.len() <= 32 &&
            strokes.iter().map(Vec::len).sum::<usize>() <= 1024 &&
            strokes.iter().all(|stroke| {
                stroke.windows(2).any(|pair| pair.first() != pair.last()) &&
                    stroke
                        .iter()
                        .flatten()
                        .all(|coordinate| *coordinate <= 1000)
            }),
        "Invalid signature trace"
    );
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SignatureImage<'a> {
    png: &'a str,
}

fn validate_signature_image(payload: &str) -> Result<()> {
    let image: SignatureImage<'_> = serde_json::from_str(payload)
        .map_err(|_| anyhow::anyhow!("Invalid signature image"))?;
    ensure!(
        valid_b64(image.png, 44, MAX_TRACE_BYTES),
        "Invalid signature image"
    );
    let png = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(image.png)
        .map_err(|_| anyhow::anyhow!("Invalid signature image"))?;
    let dimension = |offset| {
        let bytes = png.get(offset..offset + 4)?.try_into().ok()?;
        Some(u32::from_be_bytes(bytes))
    };
    ensure!(
        png.len() >= 33 &&
            png.starts_with(b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR") &&
            dimension(16).is_some_and(|n| (1..=640).contains(&n)) &&
            dimension(20).is_some_and(|n| (1..=640).contains(&n)),
        "Invalid signature image"
    );
    Ok(())
}

fn aad(tenant: &str, owner: &str, id: &str) -> Result<Vec<u8>> {
    Ok(serde_json::to_vec(&(
        "pengolo-handwritten-v1",
        tenant,
        owner,
        id,
    ))?)
}

fn seal(key: &aead::LessSafeKey, aad: &[u8], trace: &str) -> Result<Vec<u8>> {
    let mut nonce = [0; 12];
    rand::SystemRandom::new()
        .fill(&mut nonce)
        .map_err(|_| anyhow::anyhow!("Document vault unavailable"))?;
    let mut envelope =
        Vec::with_capacity(12 + trace.len() + aead::AES_256_GCM.tag_len());
    envelope.extend_from_slice(&nonce);
    envelope.extend_from_slice(trace.as_bytes());
    let (_, plaintext) = envelope.split_at_mut(12);
    let tag = key
        .seal_in_place_separate_tag(
            aead::Nonce::assume_unique_for_key(nonce),
            aead::Aad::from(aad),
            plaintext,
        )
        .map_err(|_| anyhow::anyhow!("Document vault unavailable"))?;
    envelope.extend_from_slice(tag.as_ref());
    Ok(envelope)
}

fn open(
    key: &aead::LessSafeKey,
    aad: &[u8],
    mut envelope: Vec<u8>,
) -> Result<String> {
    let (nonce, ciphertext) = envelope
        .split_at_mut_checked(12)
        .context("Invalid signature vault entry")?;
    let nonce = aead::Nonce::try_assume_unique_for_key(nonce)
        .map_err(|_| anyhow::anyhow!("Invalid signature vault entry"))?;
    let len = key
        .open_in_place(nonce, aead::Aad::from(aad), ciphertext)
        .map_err(|_| anyhow::anyhow!("Signature vault entry unavailable"))?
        .len();
    envelope.copy_within(12..12 + len, 0);
    envelope.truncate(len);
    String::from_utf8(envelope)
        .map_err(|_| anyhow::anyhow!("Invalid signature vault entry"))
}

fn mark_id(id: &str) -> Result<String> {
    Ok(uuid::Uuid::parse_str(id)
        .context("Invalid signature identifier")?
        .simple()
        .to_string())
}

impl PengoloService {
    #[tracing::instrument(skip_all)]
    pub async fn signing_vault(
        &self,
        actor: &Principal<'_>,
    ) -> Result<Option<String>> {
        self.verify(actor).await?;
        self.store.signing_vault(actor.tenant, actor.user).await
    }

    #[tracing::instrument(skip_all)]
    pub async fn create_signing_vault(
        &self,
        actor: &Principal<'_>,
        envelope: &str,
    ) -> Result<bool> {
        self.verify(actor).await?;
        validate_signing_vault(envelope)?;
        let created = self
            .store
            .create_signing_vault(actor.tenant, actor.user, envelope)
            .await?;
        tracing::info!(
            event.name = "pengolo.signing_vault.enrollment",
            created
        );
        Ok(created)
    }

    #[tracing::instrument(skip_all)]
    pub async fn signatures(
        &self,
        actor: &Principal<'_>,
    ) -> Result<Vec<SignatureMetadata>> {
        self.verify(actor).await?;
        self.store.signatures(actor.tenant, actor.user).await
    }

    #[tracing::instrument(skip_all)]
    pub async fn signature_trace(
        &self,
        actor: &Principal<'_>,
        id: &str,
    ) -> Result<Option<String>> {
        self.verify(actor).await?;
        let id = mark_id(id)?;
        let Some(ciphertext) = self
            .store
            .signature_trace(actor.tenant, actor.user, &id)
            .await?
        else {
            return Ok(None);
        };
        let trace = open(
            &document_fields::vault_key()?,
            &aad(actor.tenant, actor.user, &id)?,
            ciphertext,
        )?;
        tracing::info!(
            event.name = "pengolo.signature.viewed",
            signature.id = id
        );
        Ok(Some(trace))
    }

    #[tracing::instrument(skip_all)]
    pub async fn create_signature(
        &self,
        actor: &Principal<'_>,
        label: &str,
        trace: &str,
    ) -> Result<SignatureMetadata> {
        self.verify(actor).await?;
        let label = label.trim();
        ensure!(
            !label.is_empty() &&
                label.len() <= 120 &&
                !label.chars().any(char::is_control),
            "Invalid signature label"
        );
        validate_trace(trace)?;
        let id = uuid::Uuid::new_v4().simple().to_string();
        let ciphertext = seal(
            &document_fields::vault_key()?,
            &aad(actor.tenant, actor.user, &id)?,
            trace,
        )?;
        let saved = self
            .store
            .create_signature(
                actor.tenant,
                actor.user,
                &id,
                label,
                &ciphertext,
            )
            .await?;
        tracing::info!(
            event.name = "pengolo.signature.created",
            signature.id = id
        );
        Ok(saved)
    }

    #[tracing::instrument(skip_all)]
    pub async fn update_signature_caption(
        &self,
        actor: &Principal<'_>,
        id: &str,
        include_caption: bool,
    ) -> Result<bool> {
        self.verify(actor).await?;
        let id = mark_id(id)?;
        let updated = self
            .store
            .update_signature_caption(
                actor.tenant,
                actor.user,
                &id,
                include_caption,
            )
            .await?;
        if updated {
            tracing::info!(
                event.name = "pengolo.signature.caption_updated",
                signature.id = id
            );
        }
        Ok(updated)
    }

    pub async fn delete_signature(
        &self,
        actor: &Principal<'_>,
        id: &str,
    ) -> Result<bool> {
        self.verify(actor).await?;
        let id = mark_id(id)?;
        let deleted = self
            .store
            .delete_signature(actor.tenant, actor.user, &id)
            .await?;
        if deleted {
            tracing::info!(
                event.name = "pengolo.signature.deleted",
                signature.id = id
            );
        }
        Ok(deleted)
    }
}

#[cfg(test)]
mod tests {
    use anyhow::Result;
    use ring::aead::{AES_256_GCM, LessSafeKey, UnboundKey};

    use super::*;

    #[test]
    fn rejects_empty_unbounded_and_executable_traces() {
        for trace in ["[]", "[[[0,0],[1001,0]]]", "[[[1,1],[1,1]]]", "<svg/>"]
        {
            assert!(validate_trace(trace).is_err());
        }
        assert!(validate_trace("[[[0,0],[1000,1000]]]").is_ok());
    }

    #[test]
    fn accepts_a_bounded_png_and_rejects_other_image_payloads() {
        let png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8_x8AAwMCAO-jh2sAAAAASUVORK5CYII";
        assert!(validate_trace(&format!(r#"{{"png":"{png}"}}"#)).is_ok());
        for payload in [
            r#"{"png":"PHN2Zy8-"}"#,
            r#"{"png":"data:image/png;base64,test"}"#,
            r#"{"png":"iVBORw0KGgoAAAANSUhEUv____8AAAAB"}"#,
        ] {
            assert!(validate_trace(payload).is_err());
        }
    }

    #[test]
    fn ciphertext_cannot_be_rebound_to_another_scope_or_mark() -> Result<()> {
        let key = LessSafeKey::new(
            UnboundKey::new(&AES_256_GCM, &[7; 32])
                .map_err(|_| anyhow::anyhow!("Invalid test key"))?,
        );
        let trace = "[[[0,0],[1000,1000]]]";
        let context = aad("tenant_a", "alice", "mark_a")?;
        let ciphertext = seal(&key, &context, trace)?;
        assert_ne!(ciphertext, seal(&key, &context, trace)?);
        assert_eq!(open(&key, &context, ciphertext.clone())?, trace);
        for (tenant, owner, id) in [
            ("tenant_b", "alice", "mark_a"),
            ("tenant_a", "bob", "mark_a"),
            ("tenant_a", "alice", "mark_b"),
        ] {
            assert!(
                open(&key, &aad(tenant, owner, id)?, ciphertext.clone())
                    .is_err()
            );
        }
        let mut damaged = ciphertext;
        if let Some(byte) = damaged.last_mut() {
            *byte ^= 1;
        }
        assert!(open(&key, &context, damaged).is_err());
        Ok(())
    }
}
