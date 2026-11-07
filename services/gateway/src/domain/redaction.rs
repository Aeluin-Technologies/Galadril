//! Native redaction accepts declarative matches without storage or model
//! authority.

use anyhow::{Result, ensure};
use base64::Engine as _;
use serde::{Deserialize, Serialize};

pub const MAX_DOCUMENT_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_WIRE_BYTES: usize = 24 * 1024 * 1024;
pub const NATIVE_REDACTION_PATH: &str = "/pengolo/files/redact";

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RedactionInput {
    pub mime: String,
    pub content_base64: String,
    #[serde(deserialize_with = "bounded_rules")]
    pub rules: Vec<RedactionRule>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RedactionRule {
    kind: RuleKind,
    #[serde(default)]
    pattern: Option<String>,
    #[serde(default = "replacement")]
    replacement: String,
    #[serde(default)]
    case_sensitive: bool,
    #[serde(default)]
    replacement_mode: ReplacementMode,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum RuleKind {
    Email,
    Phone,
    Literal,
    Regex,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum ReplacementMode {
    #[default]
    Literal,
    Initials,
}

fn bounded_rules<'de, D>(
    deserializer: D,
) -> std::result::Result<Vec<RedactionRule>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    struct RulesVisitor;
    impl<'de> serde::de::Visitor<'de> for RulesVisitor {
        type Value = Vec<RedactionRule>;

        fn expecting(
            &self,
            formatter: &mut std::fmt::Formatter<'_>,
        ) -> std::fmt::Result {
            formatter.write_str("at most sixteen declarative rules")
        }

        fn visit_seq<A>(
            self,
            mut sequence: A,
        ) -> std::result::Result<Self::Value, A::Error>
        where
            A: serde::de::SeqAccess<'de>,
        {
            let mut rules = Vec::with_capacity(16);
            while let Some(rule) = sequence.next_element()? {
                if rules.len() == 16 {
                    return Err(serde::de::Error::custom(
                        "Too many redaction rules",
                    ));
                }
                rules.push(rule);
            }
            Ok(rules)
        }
    }
    deserializer.deserialize_seq(RulesVisitor)
}

fn replacement() -> String {
    "[REDACTED]".into()
}

impl RedactionInput {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            matches!(self.mime.as_str(), "text/plain" | "application/pdf"),
            "Unsupported redaction media type"
        );
        ensure!(
            !self.content_base64.is_empty() &&
                self.content_base64.len() <= MAX_WIRE_BYTES,
            "Invalid private document size"
        );
        ensure!(
            (1..=16).contains(&self.rules.len()),
            "Invalid redaction rule count"
        );
        for rule in &self.rules {
            if matches!(rule.replacement_mode, ReplacementMode::Initials) {
                ensure!(
                    matches!(rule.kind, RuleKind::Literal | RuleKind::Regex) &&
                        rule.replacement == "[REDACTED]",
                    "Invalid initials transformation"
                );
            }
            ensure!(
                (1..=64).contains(&rule.replacement.chars().count()) &&
                    !rule.replacement.chars().any(char::is_control),
                "Invalid public replacement"
            );
            match rule.kind {
                RuleKind::Email | RuleKind::Phone => ensure!(
                    rule.pattern.is_none(),
                    "Built-in pattern cannot be overridden"
                ),
                RuleKind::Literal | RuleKind::Regex => {
                    ensure!(
                        rule.pattern.as_ref().is_some_and(|pattern| (1..=512)
                            .contains(&pattern.chars().count())),
                        "Invalid redaction pattern"
                    )
                },
            }
        }
        let content = base64::engine::general_purpose::STANDARD
            .decode(&self.content_base64)
            .map_err(|_| {
                anyhow::anyhow!("Invalid private document encoding")
            })?;
        ensure!(
            !content.is_empty() && content.len() <= MAX_DOCUMENT_BYTES,
            "Invalid private document size"
        );
        ensure!(
            self.mime != "application/pdf" || content.starts_with(b"%PDF-"),
            "Invalid PDF prefix"
        );
        Ok(())
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RedactionResult {
    pub mime: String,
    pub content_base64: String,
    pub match_count: usize,
    pub pages: usize,
    pub requires_review: bool,
    pub layout_preserved: bool,
    pub coverage: String,
    pub warnings: Vec<RedactionWarning>,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RedactionWarning {
    AnnotationsRemoved,
    NoMatches,
}

impl RedactionResult {
    pub fn validate(&self, mime: &str) -> Result<Vec<u8>> {
        ensure!(
            self.mime == mime &&
                self.requires_review &&
                self.layout_preserved &&
                self.coverage == "provided_rules_only",
            "Invalid redaction guarantees"
        );
        ensure!(
            self.match_count <= 4096 &&
                self.pages <= 128 &&
                self.warnings.len() <= 2 &&
                self.content_base64.len() <= MAX_WIRE_BYTES,
            "Invalid redaction result limits"
        );
        ensure!(
            self.mime != "text/plain" || self.pages == 0,
            "Invalid text pagination"
        );
        ensure!(
            self.mime != "application/pdf" || self.pages > 0,
            "Missing PDF pages"
        );
        let content = base64::engine::general_purpose::STANDARD
            .decode(&self.content_base64)
            .map_err(|_| {
                anyhow::anyhow!("Invalid public document encoding")
            })?;
        ensure!(
            !content.is_empty() && content.len() <= MAX_DOCUMENT_BYTES,
            "Invalid public document size"
        );
        ensure!(
            self.mime != "application/pdf" || content.starts_with(b"%PDF-"),
            "Invalid public PDF prefix"
        );
        Ok(content)
    }
}

#[cfg(test)]
mod tests {
    use anyhow::Result;
    use serde_json::json;

    use super::*;

    #[test]
    fn native_requests_reject_code_authority_and_unbounded_rules() -> Result<()>
    {
        let valid = json!({"mime":"application/pdf","content_base64":"JVBERi0=","rules":[{"kind":"email"}]});
        let input: RedactionInput = serde_json::from_value(valid.clone())?;
        input.validate()?;
        for patch in [
            json!({"tenant":"other"}),
            json!({"endpoint":"https://other"}),
            json!({"rules":[{"kind":"python","pattern":"exec()"}]}),
            json!({"rules":[{"kind":"regex","pattern":""}]}),
            json!({"rules":[{"kind":"email","pattern":"override"}]}),
            json!({"rules":[{"kind":"literal","pattern":"x","replacement":"line\nfeed"}]}),
        ] {
            let mut value = valid.clone();
            if let (Some(target), Some(patch)) =
                (value.as_object_mut(), patch.as_object())
            {
                target.extend(patch.clone());
            }
            assert!(
                serde_json::from_value::<RedactionInput>(value)
                    .and_then(|input| input
                        .validate()
                        .map_err(serde::de::Error::custom))
                    .is_err()
            );
        }
        let mut input: RedactionInput = serde_json::from_value(valid)?;
        input.rules.clear();
        assert!(input.validate().is_err());
        Ok(())
    }

    #[test]
    fn rule_count_is_bounded_during_deserialization() {
        let rules = vec![json!({"kind":"email"}); 17];
        assert!(
            serde_json::from_value::<RedactionInput>(json!({
                "mime":"text/plain", "content_base64":"eA==", "rules":rules
            }))
            .is_err()
        );
    }

    #[test]
    fn initials_are_a_fixed_transformation_without_callbacks() -> Result<()> {
        let input: RedactionInput = serde_json::from_value(
            json!({"mime":"text/plain","content_base64":"bmFtZQ==","rules":[{"kind":"regex","pattern":"name","replacement_mode":"initials"}]}),
        )?;
        input.validate()?;
        Ok(())
    }

    #[test]
    fn native_results_cannot_assert_complete_anonymization_or_reflow()
    -> Result<()> {
        let valid = json!({"mime":"text/plain","content_base64":"cHVibGlj","match_count":1,"pages":0,"requires_review":true,"layout_preserved":true,"coverage":"provided_rules_only","warnings":[]});
        let result: RedactionResult = serde_json::from_value(valid.clone())?;
        assert_eq!(result.validate("text/plain")?, b"public");
        assert!(result.validate("application/pdf").is_err());
        for patch in [
            json!({"requires_review":false}),
            json!({"layout_preserved":false}),
            json!({"coverage":"all_personal_data"}),
            json!({"match_count":4097}),
            json!({"content_base64":"invalid!"}),
        ] {
            let mut value = valid.clone();
            if let (Some(target), Some(patch)) =
                (value.as_object_mut(), patch.as_object())
            {
                target.extend(patch.clone());
            }
            assert!(
                serde_json::from_value::<RedactionResult>(value)
                    .and_then(|result| result
                        .validate("text/plain")
                        .map_err(serde::de::Error::custom))
                    .is_err()
            );
        }
        Ok(())
    }
}
