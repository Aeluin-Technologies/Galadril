//! Bounded parsers for structured payloads and references to binary media.

use anyhow::{Context, Result, anyhow, bail};
use serde_json::{Map, Value, json};

/// Immutable object metadata available to every parser.
#[derive(Debug, Clone, Copy)]
pub struct ParseContext<'a> {
    pub key: &'a str,
    pub bucket: &'a str,
    pub media_type: &'a str,
}

/// Returns whether a parser needs the object bytes in process memory.
pub fn requires_content(parser_type: &str) -> bool {
    matches!(
        parser_type,
        "csv" | "tsv" | "json" | "ndjson" | "jsonl" | "text" | "sensor_json"
    )
}

/// Parses an object into JSON records accepted by route-specific Avro schemas.
pub fn parse_content(
    parser_type: &str,
    content: &[u8],
    context: &ParseContext<'_>,
) -> Result<Vec<Value>> {
    match parser_type {
        "csv" => parse_delimited(content, b','),
        "tsv" => parse_delimited(content, b'\t'),
        "json" => parse_json(content),
        "ndjson" | "jsonl" => parse_ndjson(content),
        "text" => parse_text(content),
        "sensor_json" => parse_sensor_json(content),
        "image" => parse_media_reference(context, "image"),
        "audio" => parse_media_reference(context, "audio"),
        "video" => parse_media_reference(context, "video"),
        "document" => parse_document_reference(context),
        "binary" | "metadata" | "passthrough" => {
            Ok(vec![build_reference(context)])
        },
        _ => Err(anyhow!("unknown parser type: {parser_type}")),
    }
}

fn parse_delimited(content: &[u8], delimiter: u8) -> Result<Vec<Value>> {
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(true)
        .delimiter(delimiter)
        .from_reader(content);
    let headers = reader.headers()?.clone();
    let mut records = Vec::new();

    for result in reader.records() {
        let record = result?;
        let mut map = Map::with_capacity(record.len());
        for (index, field) in record.iter().enumerate() {
            let header = headers.get(index).unwrap_or("unknown");
            let value = if let Ok(number) = field.parse::<f64>() {
                json!(number)
            } else if let Ok(boolean) = field.parse::<bool>() {
                json!(boolean)
            } else {
                json!(field)
            };
            map.insert(header.to_string(), value);
        }
        records.push(Value::Object(map));
    }
    Ok(records)
}

fn parse_json(content: &[u8]) -> Result<Vec<Value>> {
    object_records(serde_json::from_slice(content)?, "JSON")
}

fn parse_ndjson(content: &[u8]) -> Result<Vec<Value>> {
    let mut records = Vec::new();
    for (index, line) in content.split(|byte| *byte == b'\n').enumerate() {
        let trimmed = trim_ascii(line);
        if trimmed.is_empty() {
            continue;
        }
        let value: Value =
            serde_json::from_slice(trimmed).with_context(|| {
                format!("invalid NDJSON object on line {}", index + 1)
            })?;
        if !value.is_object() {
            bail!("NDJSON line {} must contain an object", index + 1);
        }
        records.push(value);
    }
    Ok(records)
}

fn parse_text(content: &[u8]) -> Result<Vec<Value>> {
    let text =
        std::str::from_utf8(content).context("text payload is not UTF-8")?;
    Ok(vec![json!({"content": text, "encoding": "utf-8"})])
}

fn parse_sensor_json(content: &[u8]) -> Result<Vec<Value>> {
    let records = parse_json(content)?;
    for (index, record) in records.iter().enumerate() {
        let has_measurement = record
            .get("measurement_type")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.trim().is_empty());
        let has_value = record.get("value").is_some() ||
            record.get("dimensions").is_some();
        if !has_measurement || !has_value {
            bail!(
                "sensor record {index} requires measurement_type and value or dimensions"
            );
        }
    }
    Ok(records)
}

fn parse_media_reference(
    context: &ParseContext<'_>,
    expected_family: &str,
) -> Result<Vec<Value>> {
    let media_type = normalized_media_type(context.media_type);
    if media_type != "application/octet-stream" &&
        !media_type.starts_with(expected_family)
    {
        bail!(
            "parser '{expected_family}' cannot accept media type '{media_type}'"
        );
    }
    Ok(vec![build_reference(context)])
}

fn parse_document_reference(context: &ParseContext<'_>) -> Result<Vec<Value>> {
    let media_type = normalized_media_type(context.media_type);
    if media_type.starts_with("image/") ||
        media_type.starts_with("audio/") ||
        media_type.starts_with("video/")
    {
        bail!("parser 'document' cannot accept media type '{media_type}'");
    }
    Ok(vec![build_reference(context)])
}

fn object_records(value: Value, format: &str) -> Result<Vec<Value>> {
    match value {
        Value::Object(_) => Ok(vec![value]),
        Value::Array(items) if items.iter().all(Value::is_object) => Ok(items),
        Value::Array(_) => bail!("{format} arrays must contain only objects"),
        _ => bail!("{format} payload must contain an object or object array"),
    }
}

fn build_reference(context: &ParseContext<'_>) -> Value {
    json!({
        "storage_path": format!("s3://{}/{}", context.bucket, context.key),
        "original_filename": context.key,
        "mime_type": normalized_media_type(context.media_type)
    })
}

fn normalized_media_type(value: &str) -> &str {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        "application/octet-stream"
    } else {
        trimmed
    }
}

fn trim_ascii(mut value: &[u8]) -> &[u8] {
    while value.first().is_some_and(u8::is_ascii_whitespace) {
        value = &value[1..];
    }
    while value.last().is_some_and(u8::is_ascii_whitespace) {
        value = &value[..value.len() - 1];
    }
    value
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONTEXT: ParseContext<'static> = ParseContext {
        key: "tenant/input/file.bin",
        bucket: "raw-input",
        media_type: "application/octet-stream",
    };

    #[test]
    fn reference_parser_preserves_notification_media_type() -> Result<()> {
        let context = ParseContext {
            key: "tenant/camera/frame.jpg",
            bucket: "raw-input",
            media_type: "image/jpeg",
        };
        let records = parse_content("image", b"", &context)?;
        assert_eq!(records[0]["mime_type"], "image/jpeg");
        assert_eq!(
            records[0]["storage_path"],
            "s3://raw-input/tenant/camera/frame.jpg"
        );
        Ok(())
    }

    #[test]
    fn parser_download_requirements_are_explicit() {
        assert!(requires_content("text"));
        assert!(requires_content("sensor_json"));
        assert!(!requires_content("video"));
    }

    #[test]
    fn parses_csv_and_tsv_records() -> Result<()> {
        let csv = parse_content("csv", b"name,active\nalice,true", &CONTEXT)?;
        let tsv = parse_content("tsv", b"name\tage\nbob\t30", &CONTEXT)?;
        assert_eq!(csv[0]["active"], true);
        assert_eq!(tsv[0]["age"], 30.0);
        Ok(())
    }

    #[test]
    fn parses_ndjson_without_copying_lines() -> Result<()> {
        let records =
            parse_content("ndjson", b"{\"id\":1}\n\n{\"id\":2}\n", &CONTEXT)?;
        assert_eq!(records.len(), 2);
        assert_eq!(records[1]["id"], 2);
        Ok(())
    }

    #[test]
    fn text_parser_validates_utf8() -> Result<()> {
        let records = parse_content("text", b"hello", &CONTEXT)?;
        assert_eq!(records[0]["content"], "hello");
        assert!(parse_content("text", &[0xff], &CONTEXT).is_err());
        Ok(())
    }

    #[test]
    fn sensor_parser_requires_measurement_shape() -> Result<()> {
        let valid = br#"{"measurement_type":"temperature","value":21.5}"#;
        assert_eq!(parse_content("sensor_json", valid, &CONTEXT)?.len(), 1);
        assert!(
            parse_content("sensor_json", br#"{"value":21.5}"#, &CONTEXT)
                .is_err()
        );
        Ok(())
    }

    #[test]
    fn rejects_non_object_json_and_media_mismatch() {
        assert!(parse_content("json", b"42", &CONTEXT).is_err());
        let context = ParseContext {
            media_type: "audio/wav",
            ..CONTEXT
        };
        assert!(parse_content("image", b"", &context).is_err());
    }
}
