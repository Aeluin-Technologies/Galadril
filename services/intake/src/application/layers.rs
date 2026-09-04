//! Observation metadata and replay-stable identifier construction.

use anyhow::{Result, anyhow};
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::application::router::ResolvedRoute;
use crate::domain::models::FileEvent;
use crate::telemetry::TraceMetadata;

const OBSERVATION_SCHEMA_VERSION: &str = "3.0.0";

/// Stable identifiers shared by records from one immutable object version.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObservationIds {
    /// One idempotent intake transaction per immutable object version.
    pub ingestion_id: String,
    /// Correlation identifier for the source object.
    pub source_object_id: String,
}

/// Creates replay-stable identifiers without mutable counters or random state.
pub fn observation_ids(event: &FileEvent) -> ObservationIds {
    let object_version = format!(
        "s3://{}/{}#{}",
        event.bucket,
        event.key,
        event.e_tag.trim_matches('"')
    );
    ObservationIds {
        ingestion_id: stable_id("ingestion", &object_version),
        source_object_id: stable_id("source-object", &object_version),
    }
}

/// Validates and attaches the shared LI-ESKG observation contract to a record.
pub fn enrich_record(
    record: &mut Value,
    event: &FileEvent,
    route: &ResolvedRoute,
    ids: &ObservationIds,
    trace: &TraceMetadata,
    ordinal: usize,
) -> Result<String> {
    let (observation_id, event_time) = {
        let object = record
            .as_object_mut()
            .ok_or_else(|| anyhow!("record must be a JSON object"))?;
        let fragment_key = object
            .get("id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(ToOwned::to_owned)
            .unwrap_or_else(|| ordinal.to_string());
        let observation_id = stable_id(
            "observation",
            &format!("{}:{fragment_key}", ids.source_object_id),
        );

        match object.get("id") {
            Some(Value::String(value)) if !value.is_empty() => {},
            Some(_) => {
                return Err(anyhow!("record 'id' must be a string"));
            },
            None => {
                object.insert(
                    "id".to_string(),
                    Value::String(observation_id.clone()),
                );
            },
        }

        let event_time = numeric_i64(object.get("timestamp"))
            .unwrap_or_else(|| event.received_at.timestamp_millis());
        object.insert(
            "ingested_at".to_string(),
            json!(event.received_at.timestamp_millis()),
        );
        object
            .entry("storage_path".to_string())
            .or_insert_with(|| Value::String(storage_uri(event)));
        object
            .entry("source".to_string())
            .or_insert_with(|| Value::String(route.source_id.clone()));
        (observation_id, event_time)
    };

    let context = observation_context(
        event,
        route,
        ids,
        trace,
        &observation_id,
        Some(record),
        Some(ordinal),
    );
    let object = record
        .as_object_mut()
        .ok_or_else(|| anyhow!("record must remain a JSON object"))?;
    object
        .entry("timestamp".to_string())
        .or_insert_with(|| json!(event_time));
    object.insert("observation".to_string(), context);
    Ok(observation_id)
}

fn observation_context(
    event: &FileEvent,
    route: &ResolvedRoute,
    ids: &ObservationIds,
    trace: &TraceMetadata,
    observation_id: &str,
    record: Option<&Value>,
    ordinal: Option<usize>,
) -> Value {
    let source_event_time =
        record.and_then(|value| numeric_i64(value.get("timestamp")));
    let event_time = source_event_time
        .unwrap_or_else(|| event.received_at.timestamp_millis());
    let event_time_end =
        record.and_then(|value| numeric_i64(value.get("event_time_end")));
    let input_type = classify_input_type(&event.content_type, &event.key);
    let fragment_id = ordinal.map(|value| format!("row:{value}"));

    json!({
        "observation_id": observation_id,
        "source": {
            "source_id": route.source_id,
            "source_kind": route.source_kind,
            "sensor_id": record_string(record, "sensor_id").or(route.sensor_id.as_deref()),
            "sensor_type": record_string(record, "sensor_type").or(route.sensor_type.as_deref()),
            "device_id": record_string(record, "device_id").or(route.device_id.as_deref()),
            "capture_id": record_string(record, "capture_id"),
            "sequence_number": record.and_then(|value| numeric_i64(value.get("sequence_number"))),
            "original_filename": event.key,
            "bucket": event.bucket,
            "object_key": event.key
        },
        "input_type": input_type,
        "event_time": event_time,
        "event_time_end": event_time_end,
        "ingestion_time": event.received_at.timestamp_millis(),
        "payload": {
            "uri": storage_uri(event),
            "media_type": normalized_media_type(&event.content_type),
            "size_bytes": event.size.max(0),
            "content_hash": normalized_etag(&event.e_tag),
            "hash_algorithm": "S3_ETAG",
            "encoding": record_string(record, "encoding"),
            "byte_offset": record.and_then(|value| numeric_i64(value.get("byte_offset"))),
            "byte_length": record.and_then(|value| numeric_i64(value.get("byte_length")))
        },
        "quality": quality_metadata(record, source_event_time.is_some()),
        "spatial": spatial_metadata(record),
        "lineage": {
            "ingestion_id": ids.ingestion_id,
            "trace_id": trace.trace_id,
            "span_id": trace.span_id,
            "traceparent": trace.traceparent,
            "tracestate": trace.tracestate,
            "source_event_id": source_event_id(event),
            "correlation_id": ids.source_object_id,
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "parent_observation_ids": [],
            "supersedes_observation_id": record_string(record, "supersedes_observation_id"),
            "idempotency_key": observation_id
        },
        "fragment_id": fragment_id,
        "concurrent_group_id": record_string(record, "concurrent_group_id")
            .or_else(|| record_string(record, "capture_id"))
            .or(Some(ids.source_object_id.as_str()))
    })
}

fn quality_metadata(
    record: Option<&Value>,
    source_event_time_present: bool,
) -> Value {
    let covariance = record
        .and_then(|value| value.get("covariance"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    json!({
        "confidence": record.and_then(|value| numeric_f64(value.get("confidence"))),
        "calibration_id": record_string(record, "calibration_id"),
        "localization_confidence": record.and_then(|value| numeric_f64(value.get("localization_confidence"))),
        "signal_to_noise_db": record.and_then(|value| numeric_f64(value.get("signal_to_noise_db"))),
        "sample_rate_hz": record.and_then(|value| numeric_f64(value.get("sample_rate_hz"))),
        "frame_index": record.and_then(|value| numeric_i64(value.get("frame_index"))),
        "text_span_start": record.and_then(|value| numeric_i64(value.get("text_span_start"))),
        "text_span_end": record.and_then(|value| numeric_i64(value.get("text_span_end"))),
        "covariance": covariance,
        "attributes": Map::from_iter([
            (
                "event_time_source".to_string(),
                Value::String(
                    if source_event_time_present {
                        "source_payload"
                    } else {
                        "ingestion_fallback"
                    }
                    .to_string(),
                ),
            ),
            (
                "content_hash_semantics".to_string(),
                Value::String("storage_fingerprint".to_string()),
            ),
        ])
    })
}

fn spatial_metadata(record: Option<&Value>) -> Option<Value> {
    let value = record?;
    let latitude = numeric_f64(value.get("latitude"));
    let longitude = numeric_f64(value.get("longitude"));
    if let (Some(latitude), Some(longitude)) = (latitude, longitude) {
        return Some(json!({
            "reference_system": record_string(record, "reference_system").unwrap_or("WGS84"),
            "latitude": latitude,
            "longitude": longitude,
            "altitude_meters": numeric_f64(value.get("altitude_meters")),
            "accuracy_meters": numeric_f64(value.get("accuracy_meters")),
            "geometry_wkt": record_string(record, "geometry_wkt"),
            "covariance": value.get("spatial_covariance").and_then(Value::as_array).cloned().unwrap_or_default()
        }));
    }

    let geometry = value.get("geometry")?.as_object()?;
    let top = numeric_f64(geometry.get("top_left_lat"))?;
    let left = numeric_f64(geometry.get("top_left_lon"))?;
    let bottom = numeric_f64(geometry.get("bottom_right_lat"))?;
    let right = numeric_f64(geometry.get("bottom_right_lon"))?;
    Some(json!({
        "reference_system": "WGS84",
        "latitude": (top + bottom) / 2.0,
        "longitude": (left + right) / 2.0,
        "altitude_meters": null,
        "accuracy_meters": null,
        "geometry_wkt": null,
        "covariance": []
    }))
}

fn classify_input_type(media_type: &str, key: &str) -> &'static str {
    let normalized_media_type = media_type.trim().to_ascii_lowercase();
    match normalized_media_type.as_str() {
        "text/csv" | "text/tab-separated-values" | "application/csv" => {
            "TABULAR"
        },
        "application/json" | "application/x-ndjson" => "STRUCTURED",
        "application/pdf" => "DOCUMENT",
        value if value.starts_with("text/") => "TEXT",
        value if value.starts_with("image/") => "IMAGE",
        value if value.starts_with("video/") => "VIDEO",
        value if value.starts_with("audio/") => "AUDIO",
        _ => classify_input_type_from_extension(key),
    }
}

fn classify_input_type_from_extension(key: &str) -> &'static str {
    let extension = key.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    match extension.as_str() {
        "csv" | "tsv" => "TABULAR",
        "json" | "jsonl" | "ndjson" | "avro" => "STRUCTURED",
        "txt" | "md" | "html" | "xml" => "TEXT",
        "pdf" | "doc" | "docx" | "odt" => "DOCUMENT",
        "jpg" | "jpeg" | "png" | "gif" | "webp" | "tif" | "tiff" => "IMAGE",
        "wav" | "mp3" | "flac" | "ogg" | "m4a" => "AUDIO",
        "mp4" | "mov" | "mkv" | "webm" => "VIDEO",
        "pcd" | "ply" | "las" | "laz" => "POINT_CLOUD",
        _ => "BINARY",
    }
}

fn record_string<'a>(record: Option<&'a Value>, key: &str) -> Option<&'a str> {
    record?.get(key)?.as_str().filter(|value| !value.is_empty())
}

fn numeric_i64(value: Option<&Value>) -> Option<i64> {
    value.and_then(|item| {
        item.as_i64()
            .or_else(|| item.as_f64().map(|number| number as i64))
    })
}

fn numeric_f64(value: Option<&Value>) -> Option<f64> {
    value
        .and_then(Value::as_f64)
        .filter(|number| number.is_finite())
}

fn normalized_etag(value: &str) -> &str {
    let normalized = value.trim().trim_matches('"');
    if normalized.is_empty() {
        "unavailable"
    } else {
        normalized
    }
}

fn normalized_media_type(value: &str) -> &str {
    let normalized = value.trim();
    if normalized.is_empty() {
        "application/octet-stream"
    } else {
        normalized
    }
}

fn storage_uri(event: &FileEvent) -> String {
    format!("s3://{}/{}", event.bucket, event.key)
}

fn source_event_id(event: &FileEvent) -> String {
    stable_id(
        "source-event",
        &format!(
            "{}:s3://{}/{}#{}",
            event.event_name,
            event.bucket,
            event.key,
            normalized_etag(&event.e_tag)
        ),
    )
}

fn stable_id(kind: &str, value: &str) -> String {
    Uuid::new_v5(&Uuid::NAMESPACE_URL, format!("{kind}:{value}").as_bytes())
        .to_string()
}

#[cfg(test)]
mod tests {
    use chrono::Utc;

    use super::*;

    fn event() -> FileEvent {
        FileEvent {
            bucket: "bronze".to_string(),
            key: "tenant/camera/frame.jpg".to_string(),
            size: 42,
            e_tag: "\"abc123\"".to_string(),
            content_type: "image/jpeg".to_string(),
            event_name: "s3:ObjectCreated:Put".to_string(),
            received_at: Utc::now(),
        }
    }

    fn route() -> ResolvedRoute {
        ResolvedRoute {
            identity: crate::domain::ports::PipelineIdentity {
                tenant_id: "tenant".to_owned(),
                pipeline_id: "daily".to_owned(),
                revision_id: "revision_1".to_owned(),
            },
            source_id: "camera-east".to_string(),
            topic: "vision.silver".to_string(),
            schema_path: Some("image.avsc".to_string()),
            parser: "metadata".to_string(),
            source_kind: "camera".to_string(),
            sensor_id: Some("cam-1".to_string()),
            sensor_type: Some("rgb".to_string()),
            device_id: None,
        }
    }

    #[test]
    fn ids_are_stable_for_object_version() {
        assert_eq!(observation_ids(&event()), observation_ids(&event()));
    }

    #[test]
    fn context_classifies_input_and_preserves_uncertainty() -> Result<()> {
        let event = event();
        let route = route();
        let ids = observation_ids(&event);
        let mut record =
            json!({"confidence": 0.8, "covariance": [1.0, 0.0, 0.0, 1.0]});
        let observation_id = enrich_record(
            &mut record,
            &event,
            &route,
            &ids,
            &TraceMetadata::default(),
            0,
        )?;

        assert_eq!(record["observation"]["observation_id"], observation_id);
        assert_eq!(record["observation"]["input_type"], "IMAGE");
        assert_eq!(
            record["observation"]["lineage"]["correlation_id"],
            ids.source_object_id
        );
        assert_eq!(record["observation"]["quality"]["confidence"], 0.8);
        assert_eq!(
            record["observation"]["quality"]["attributes"]["event_time_source"],
            "ingestion_fallback"
        );
        Ok(())
    }
}
