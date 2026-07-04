//! Data parser.

use anyhow::{Result, anyhow};
use chrono::Utc;
use serde_json::{Value, json};
use uuid::Uuid;

/// Parse content into generic JSON payloads matching the unified ESKG schemas.
pub fn parse_content(
    parser_type: &str,
    content: &[u8],
    key: &str,
    bucket: &str,
) -> Result<Vec<Value>> {
    match parser_type {
        "csv" => parse_csv(content, key, bucket),
        "json" => parse_json(content, key, bucket),
        "metadata" | "passthrough" => Ok(vec![build_metadata(key, bucket)]),
        _ => Err(anyhow!("Unknown parser type: {}", parser_type)),
    }
}

fn parse_csv(content: &[u8], key: &str, bucket: &str) -> Result<Vec<Value>> {
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(true)
        .from_reader(content);
    let headers = reader.headers()?.clone();
    let mut records = Vec::new();

    let now_millis = Utc::now().timestamp_millis();
    let source = key.split('/').next().unwrap_or(bucket);

    for result in reader.records() {
        let record = result?;
        let mut map = serde_json::Map::new();

        for (i, field) in record.iter().enumerate() {
            let header = headers.get(i).unwrap_or("unknown");

            let val = if let Ok(num) = field.parse::<f64>() {
                json!(num)
            } else if let Ok(b) = field.parse::<bool>() {
                json!(b)
            } else {
                json!(field)
            };
            map.insert(header.to_string(), val);
        }

        if !map.contains_key("id") {
            map.insert("id".to_string(), json!(Uuid::new_v4().to_string()));
        }
        if !map.contains_key("timestamp") {
            map.insert("timestamp".to_string(), json!(now_millis));
        }
        if !map.contains_key("ingested_at") {
            map.insert("ingested_at".to_string(), json!(now_millis));
        }
        if !map.contains_key("source") {
            map.insert("source".to_string(), json!(source));
        }

        records.push(Value::Object(map));
    }
    Ok(records)
}

fn parse_json(content: &[u8], key: &str, bucket: &str) -> Result<Vec<Value>> {
    let mut parsed: Value = serde_json::from_slice(content)?;
    let now_millis = Utc::now().timestamp_millis();
    let source = key.split('/').next().unwrap_or(bucket);

    let inject_fields = |obj: &mut serde_json::Map<String, Value>| {
        if !obj.contains_key("id") {
            obj.insert("id".to_string(), json!(Uuid::new_v4().to_string()));
        }
        if !obj.contains_key("timestamp") {
            obj.insert("timestamp".to_string(), json!(now_millis));
        }
        if !obj.contains_key("ingested_at") {
            obj.insert("ingested_at".to_string(), json!(now_millis));
        }
        if !obj.contains_key("source") {
            obj.insert("source".to_string(), json!(source));
        }
    };

    if let Value::Array(ref mut arr) = parsed {
        for item in arr.iter_mut() {
            if let Value::Object(obj) = item {
                inject_fields(obj);
            }
        }
        Ok(parsed.as_array().unwrap().clone())
    } else if let Value::Object(ref mut obj) = parsed {
        inject_fields(obj);
        Ok(vec![parsed])
    } else {
        Ok(vec![parsed])
    }
}

fn build_metadata(key: &str, bucket: &str) -> Value {
    let storage_path = format!("s3://{}/{}", bucket, key);
    let common_id = Uuid::new_v4().to_string();
    let source = key.split('/').next().unwrap_or(bucket);
    let now_millis = Utc::now().timestamp_millis();

    // We inject all possible IDs so the unified schema works for everything.
    json!({
        "id": common_id,
        "timestamp": now_millis,
        "ingested_at": now_millis,
        "storage_path": storage_path,
        "source": source,
        "original_filename": key,
        "mime_type": "application/octet-stream"
    })
}

#[cfg(test)]
mod parser_tests {
    use super::*;

    #[test]
    fn test_parse_metadata_passthrough() {
        let res =
            parse_content("metadata", b"", "tenant1/g1/file.txt", "my-bucket")
                .unwrap();
        assert_eq!(res.len(), 1);
        assert_eq!(res[0]["source"], "tenant1");
        assert_eq!(res[0]["original_filename"], "tenant1/g1/file.txt");
    }

    #[test]
    fn test_parse_json_object_injection() {
        let json_data = b"{\"custom_field\": \"value\"}";
        let res = parse_content(
            "json",
            json_data,
            "tenant1/g1/data.json",
            "my-bucket",
        )
        .unwrap();
        assert_eq!(res.len(), 1);
        assert_eq!(res[0]["custom_field"], "value");
        assert!(res[0].get("id").is_some());
        assert!(res[0].get("ingested_at").is_some());
    }

    #[test]
    fn test_parse_csv_records() {
        let csv_data = b"name,age,active\nalice,24.5,true\nbob,30,false";
        let res = parse_content(
            "csv",
            csv_data,
            "tenant1/g1/users.csv",
            "my-bucket",
        )
        .unwrap();
        assert_eq!(res.len(), 2);
        assert_eq!(res[0]["name"], "alice");
        assert_eq!(res[0]["age"], 24.5);
        assert_eq!(res[0]["active"], true);
    }

    #[test]
    fn test_parse_unknown_type_error() {
        let res = parse_content("invalid_parser", b"", "key", "bucket");
        assert!(res.is_err());
    }
}
