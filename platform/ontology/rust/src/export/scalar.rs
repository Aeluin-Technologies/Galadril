//! Preserve value ranges across explicit JSON representations.

use serde_json::{Value, json};

use super::{ExportError, ExportOptions, IntegerEncoding, SchemaProfile};
use crate::Primitive;

pub(super) struct Offline;

impl jsonschema::Retrieve for Offline {
    fn retrieve(
        &self,
        _uri: &jsonschema::Uri<String>,
    ) -> Result<Value, Box<dyn std::error::Error + Send + Sync>> {
        Err(Box::new(ExportError::Unavailable))
    }
}

pub(super) fn validator(
    schema: &Value,
) -> Result<jsonschema::Validator, ExportError> {
    jsonschema::draft202012::options()
        .with_retriever(Offline)
        .should_validate_formats(true)
        .build(schema)
        .map_err(|_| ExportError::Mapping)
}

pub(super) fn bounded_json(
    value: &Value,
    max_depth: usize,
    max_bytes: usize,
) -> Result<(), ExportError> {
    let mut work = vec![(value, 0)];
    let mut size = 0usize;
    while let Some((value, depth)) = work.pop() {
        if depth > max_depth || size > max_bytes {
            return Err(ExportError::Limit);
        }
        size = size.saturating_add(8);
        match value {
            Value::String(text) => size = size.saturating_add(text.len()),
            Value::Array(items) => {
                if items.len() > max_bytes.saturating_sub(size) / 2 {
                    return Err(ExportError::Limit);
                }
                work.extend(items.iter().map(|item| (item, depth + 1)));
            },
            Value::Object(properties) => {
                if properties.len() > max_bytes.saturating_sub(size) / 2 {
                    return Err(ExportError::Limit);
                }
                for (key, value) in properties {
                    size = size.saturating_add(key.len());
                    work.push((value, depth + 1));
                }
            },
            _ => {},
        }
    }
    if size > max_bytes ||
        serde_json::to_vec(value)
            .map_err(|_| ExportError::Limit)?
            .len() >
            max_bytes
    {
        return Err(ExportError::Limit);
    }
    Ok(())
}

pub(super) fn primitive(
    ty: Primitive,
    options: &ExportOptions,
) -> Result<Value, ExportError> {
    let (negative, maximum) = match ty {
        Primitive::I8 => {
            (Some(i8::MIN.unsigned_abs() as u128), i8::MAX as u128)
        },
        Primitive::I16 => {
            (Some(i16::MIN.unsigned_abs() as u128), i16::MAX as u128)
        },
        Primitive::I32 => {
            (Some(i32::MIN.unsigned_abs() as u128), i32::MAX as u128)
        },
        Primitive::I64 => {
            (Some(i64::MIN.unsigned_abs() as u128), i64::MAX as u128)
        },
        Primitive::I128 => (Some(i128::MIN.unsigned_abs()), i128::MAX as u128),
        Primitive::U8 => (None, u8::MAX as u128),
        Primitive::U16 => (None, u16::MAX as u128),
        Primitive::U32 => (None, u32::MAX as u128),
        Primitive::U64 => (None, u64::MAX as u128),
        Primitive::U128 => (None, u128::MAX),
        Primitive::Bool => return Ok(json!({"type": "boolean"})),
        Primitive::Unit => return Ok(json!({"type": "null"})),
        Primitive::String => return Ok(json!({"type": "string"})),
        Primitive::Char => {
            return Ok(
                json!({"type": "string", "minLength": 1, "maxLength": 1}),
            );
        },
        Primitive::F32 | Primitive::F64 => {
            if !options.allow_float_rounding {
                return Err(ExportError::Precision);
            }
            if options.profile == SchemaProfile::Outlines {
                return Err(ExportError::Profile);
            }
            let maximum = if ty == Primitive::F32 {
                f32::MAX as f64
            } else {
                f64::MAX
            };
            return Ok(
                json!({"type": "number", "minimum": -maximum, "maximum": maximum}),
            );
        },
    };
    if options.integers == IntegerEncoding::DecimalStrings {
        let positive = up_to(maximum, true);
        let pattern = negative.map_or_else(
            || format!("^(?:{positive})$"),
            |negative| {
                format!("^(?:{positive}|-(?:{}))$", up_to(negative, false))
            },
        );
        return Ok(json!({"type": "string", "pattern": pattern}));
    }
    if maximum > (1_u128 << 53) - 1 {
        return Err(ExportError::Precision);
    }
    if options.profile == SchemaProfile::Outlines {
        // Outlines 0.2.14 parses numeric types without enforcing minimum or
        // maximum. Changing to strings requires the explicit encoding option.
        return Err(ExportError::Profile);
    }
    let minimum = negative.map_or(0, |magnitude| -(magnitude as i64));
    Ok(
        json!({"type": "integer", "minimum": minimum, "maximum": maximum as u64}),
    )
}

fn up_to(maximum: u128, zero: bool) -> String {
    let digits = maximum.to_string();
    let mut alternatives = Vec::new();
    if zero {
        alternatives.push("0".to_owned());
    }
    if digits.len() > 1 {
        alternatives.push(format!("[1-9][0-9]{{0,{}}}", digits.len() - 2));
    }
    let mut prefix = String::new();
    for (index, digit) in digits.bytes().enumerate() {
        let low = if index == 0 { b'1' } else { b'0' };
        if digit > low {
            let below = digit - 1;
            let range = if low == below {
                char::from(low).to_string()
            } else {
                format!("[{}-{}]", char::from(low), char::from(below))
            };
            alternatives.push(format!(
                "{prefix}{range}[0-9]{{{}}}",
                digits.len() - index - 1
            ));
        }
        prefix.push(char::from(digit));
    }
    alternatives.push(digits);
    alternatives.join("|")
}

pub(super) fn validate_mapping(schema: &Value) -> Result<(), ExportError> {
    bounded_json(schema, 4, 16 * 1024)?;
    let object = schema.as_object().ok_or(ExportError::Mapping)?;
    if !matches!(
        object.get("type").and_then(Value::as_str),
        Some("string" | "boolean" | "integer" | "number")
    ) || !object.keys().all(|key| {
        matches!(
            key.as_str(),
            "type" |
                "format" |
                "pattern" |
                "minLength" |
                "maxLength" |
                "minimum" |
                "maximum" |
                "enum"
        )
    }) {
        return Err(ExportError::Mapping);
    }
    if let Some(format) = object.get("format") &&
        !matches!(
            format.as_str(),
            Some("date-time" | "date" | "time" | "uuid")
        )
    {
        return Err(ExportError::Mapping);
    }
    if matches!(object.get("type").and_then(Value::as_str), Some("integer")) &&
        (!object
            .get("minimum")
            .and_then(Value::as_i64)
            .is_some_and(|min| min.unsigned_abs() < (1 << 53)) ||
            !object
                .get("maximum")
                .and_then(Value::as_u64)
                .is_some_and(|max| max < (1 << 53)))
    {
        return Err(ExportError::Precision);
    }
    validator(schema)?;
    Ok(())
}

pub(super) fn compatible(
    value: &Value,
    profile: SchemaProfile,
) -> Result<(), ExportError> {
    let Some(object) = value.as_object() else {
        return Ok(());
    };
    if profile == SchemaProfile::Outlines {
        if object.contains_key("minimum") || object.contains_key("maximum") {
            return Err(ExportError::Profile);
        }
        let string_constraints = ["pattern", "format"]
            .iter()
            .filter(|key| object.contains_key(**key))
            .count() +
            usize::from(
                object.contains_key("minLength") ||
                    object.contains_key("maxLength"),
            );
        if string_constraints > 1 ||
            (object.contains_key("enum") && string_constraints > 0)
        {
            return Err(ExportError::Profile);
        }
        if let Some(values) = object.get("enum").and_then(Value::as_array) {
            let validator = validator(value)?;
            if values.iter().any(|value| !validator.is_valid(value)) {
                return Err(ExportError::Profile);
            }
        }
    }
    if profile == SchemaProfile::OpenAiStrict &&
        (object.contains_key("minLength") ||
            object.contains_key("maxLength"))
    {
        return Err(ExportError::Profile);
    }
    // Property and definition names are data, even when they happen to be
    // JSON Schema keywords. Only visit actual subschema positions.
    for key in ["properties", "$defs"] {
        if let Some(values) = object.get(key).and_then(Value::as_object) {
            for value in values.values() {
                compatible(value, profile)?;
            }
        }
    }
    if let Some(items) = object.get("items") {
        compatible(items, profile)?;
    }
    if let Some(values) = object.get("anyOf").and_then(Value::as_array) {
        for value in values {
            compatible(value, profile)?;
        }
    }
    Ok(())
}
