//! Sanitization and S3 key construction for HTTP uploads.

use anyhow::{Result, bail};

const MAX_TENANT_LEN: usize = 64;
const MAX_GROUP_LEN: usize = 64;
const MAX_NAME_LEN: usize = 256;

/// Sanitized upload description (tenant-scoped).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SanitizedUpload {
    pub tenant_id: String,
    pub group_id: String,
    pub object_name: String,
    pub s3_key: String,
}

impl SanitizedUpload {
    pub fn tenant_bucket_resource_id(&self) -> String {
        format!("tenant:{}", self.tenant_id)
    }
}

pub fn sanitize_upload_request(
    tenant_id: &str,
    group_id: Option<&str>,
    name: &str,
) -> Result<SanitizedUpload> {
    let tenant_id = sanitize_component(tenant_id, MAX_TENANT_LEN, true)?;
    let group_id = sanitize_component(
        group_id.unwrap_or("default"),
        MAX_GROUP_LEN,
        true,
    )?;
    let object_name = sanitize_component(name, MAX_NAME_LEN, false)?;
    let s3_key = format!("{tenant_id}/{group_id}/{object_name}");

    Ok(SanitizedUpload {
        tenant_id,
        group_id,
        object_name,
        s3_key,
    })
}

fn sanitize_component(
    input: &str,
    max_len: usize,
    strict: bool,
) -> Result<String> {
    let trimmed = input.trim();

    if trimmed.is_empty() {
        bail!("component is empty");
    }
    if trimmed.len() > max_len {
        bail!("component exceeds maximum allowed length");
    }

    if strict {
        if !trimmed
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        {
            bail!("strict component contains invalid characters");
        }
    } else {
        if trimmed.starts_with('.') || trimmed.ends_with('.') {
            bail!("object name cannot start or end with a dot");
        }
        if !trimmed.bytes().all(|b| {
            b.is_ascii_alphanumeric() || b == b'_' || b == b'-' || b == b'.'
        }) {
            bail!("object name contains invalid characters");
        }
    }

    let upper = trimmed.to_uppercase();
    if matches!(
        upper.as_str(),
        "CON" |
            "PRN" |
            "AUX" |
            "NUL" |
            "COM1" |
            "COM2" |
            "COM3" |
            "COM4" |
            "COM5" |
            "COM6" |
            "COM7" |
            "COM8" |
            "COM9" |
            "LPT1" |
            "LPT2" |
            "LPT3" |
            "LPT4" |
            "LPT5" |
            "LPT6" |
            "LPT7" |
            "LPT8" |
            "LPT9"
    ) {
        bail!("component uses a reserved OS name");
    }

    let sanitized = sanitize_filename::sanitize(trimmed);
    if sanitized != trimmed {
        bail!("component failed secondary sanitization checks");
    }

    Ok(trimmed.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitize_builds_key() {
        let s = sanitize_upload_request("t1", Some("g1"), "file.bin").unwrap();
        assert_eq!(s.s3_key, "t1/g1/file.bin");
    }

    #[test]
    fn sanitize_rejects_traversal() {
        assert!(sanitize_upload_request("t1", Some("g1"), "../x").is_err());
        assert!(sanitize_upload_request("t1", Some("g1"), "a/b").is_err());
        assert!(sanitize_upload_request("t1", Some("g1"), r"a\b").is_err());
        assert!(sanitize_upload_request("t1", Some("g1"), "..").is_err());
        assert!(sanitize_upload_request("t1", Some("g1"), ".").is_err());
    }

    #[test]
    fn sanitize_rejects_malicious_os_names() {
        assert!(
            sanitize_upload_request("CON", Some("g1"), "file.bin").is_err()
        );
        assert!(sanitize_upload_request("t1", Some("g1"), "NUL").is_err());
        assert!(
            sanitize_upload_request("t1", Some("g1"), "file\0name.bin")
                .is_err()
        );
    }

    #[test]
    fn sanitize_rejects_unstable_dots() {
        assert!(sanitize_upload_request("t1", Some("g1"), ".hidden").is_err());
        assert!(sanitize_upload_request("t1", Some("g1"), "file.").is_err());
    }

    #[test]
    fn sanitize_defaults_group() {
        let s = sanitize_upload_request("t1", None, "file.bin").unwrap();
        assert_eq!(s.group_id, "default");
    }
}
