//! Build closed schemas from independently approved metadata nodes.

use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::pin::Pin;

use serde_json::{Map, Value, json};

use super::model::{Definition, Field};
use super::source::Source;
use super::{
    ExportError, ExportOptions, ExportRequest, ExternalMappings,
    MappingPrecision, Projection, SchemaDocument, SchemaPart, SchemaProfile,
    Selection, access_for, is_description, scalar,
};
use crate::authorization::{PermissionProvider, TrustedRuntime};
use crate::{FieldRef, Name, Reference, SymbolRef, TypeRef};

pub(super) async fn export(
    source: &impl Source,
    request: &ExportRequest<'_>,
    runtime: &(impl TrustedRuntime + Sync),
    provider: &(impl PermissionProvider + Sync),
) -> Result<SchemaDocument, ExportError> {
    let projection = request.projection;
    let part = request.part;
    let options = request.options;
    let mappings = request.mappings;
    options.validate()?;
    if projection.root.fields.len() > options.max_properties ||
        projection.definitions.len() > 1024
    {
        return Err(ExportError::Limit);
    }
    if !is_description(projection.root.access.route.operation) {
        return Err(ExportError::Unavailable);
    }
    let mut builder = Builder {
        source,
        projection,
        options,
        mappings,
        runtime,
        provider,
        definitions: Map::new(),
        optional_aliases: BTreeSet::new(),
        active: BTreeSet::new(),
        count: 0,
    };
    let target = &projection.root.access.route.target;
    let owner = owner(target);
    if !builder.approved(&projection.root).await? {
        return Err(ExportError::Unavailable);
    }
    let definition = source.definition(owner).await?;
    let mut emitted = BTreeSet::new();
    let schema = match (part, target, definition) {
        (
            SchemaPart::Value,
            Reference::Field(field),
            Definition::Entity { mut fields } |
            Definition::Struct { mut fields },
        ) => {
            let field =
                fields.remove(&field.name).ok_or(ExportError::Unavailable)?;
            builder
                .value(&field.kind.value_type()?, 0)
                .await?
                .ok_or(ExportError::Unavailable)?
        },
        (
            SchemaPart::Value,
            Reference::Symbol(_),
            Definition::Entity { fields } | Definition::Struct { fields },
        ) => {
            let properties =
                builder.fields(&projection.root, fields, 0).await?;
            if properties.is_empty() && !projection.root.fields.is_empty() {
                return Err(ExportError::Unavailable);
            }
            emitted.extend(
                properties
                    .keys()
                    .map(|name| name.parse())
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(|_| ExportError::Unavailable)?,
            );
            builder.object(properties)
        },
        (
            SchemaPart::ActionInput,
            Reference::Symbol(_),
            Definition::Action { parameters, .. },
        ) => {
            // Parameter annotations are absent from the canonical signature.
            // The explicit action description controls its selected
            // parameters.
            let mut properties = BTreeMap::new();
            if parameters.iter().any(|(name, ty)| {
                !optional(ty) && !projection.root.fields.contains(name)
            }) {
                return Err(ExportError::Unsupported);
            }
            for name in &projection.root.fields {
                if let Some(ty) = parameters.get(name) {
                    let value = builder
                        .value(ty, 1)
                        .await?
                        .ok_or(ExportError::Unavailable)?;
                    properties.insert(
                        name.to_string(),
                        (value, builder.optional(ty)),
                    );
                }
            }
            builder.object(properties)
        },
        (
            SchemaPart::ActionOutput,
            Reference::Symbol(_),
            Definition::Action { result, .. },
        ) => {
            let value = builder
                .value(&result, 0)
                .await?
                .ok_or(ExportError::Unavailable)?;
            builder.object(BTreeMap::from([("result".into(), (value, false))]))
        },
        (SchemaPart::Value, Reference::Symbol(_), other) => builder
            .nominal(other, &projection.root, 0)
            .await?
            .ok_or(ExportError::Unavailable)?,
        _ => return Err(ExportError::Unsupported),
    };
    let mut schema = schema
        .as_object()
        .cloned()
        .ok_or(ExportError::Unsupported)?;
    if options.profile == SchemaProfile::OpenAiStrict &&
        schema.get("type").and_then(Value::as_str) != Some("object")
    {
        return Err(ExportError::Profile);
    }
    schema.insert(
        "$schema".into(),
        Value::String("https://json-schema.org/draft/2020-12/schema".into()),
    );
    if !builder.definitions.is_empty() {
        schema.insert("$defs".into(), Value::Object(builder.definitions));
    }
    let mut schema = Value::Object(schema);
    schema.sort_all_objects();
    scalar::bounded_json(&schema, 64, options.max_bytes)?;
    scalar::compatible(&schema, options.profile)?;
    let validator = scalar::validator(&schema)?;
    Ok(SchemaDocument {
        schema,
        validator,
        options: options.clone(),
        emitted,
    })
}

fn owner(reference: &Reference) -> &SymbolRef {
    match reference {
        Reference::Symbol(symbol) => symbol,
        Reference::Field(field) => &field.owner,
    }
}

fn optional(ty: &TypeRef) -> bool {
    matches!(ty, TypeRef::Option(_))
}

struct Builder<'a, S, R, P> {
    source: &'a S,
    projection: &'a Projection,
    options: &'a ExportOptions,
    mappings: &'a ExternalMappings,
    runtime: &'a R,
    provider: &'a P,
    definitions: Map<String, Value>,
    optional_aliases: BTreeSet<SymbolRef>,
    active: BTreeSet<SymbolRef>,
    count: usize,
}

impl<S: Source, R: TrustedRuntime + Sync, P: PermissionProvider + Sync>
    Builder<'_, S, R, P>
{
    fn optional(&self, ty: &TypeRef) -> bool {
        optional(ty) ||
            matches!(ty, TypeRef::Named(symbol) if self.optional_aliases.contains(symbol))
    }

    async fn approved(
        &self,
        selection: &Selection,
    ) -> Result<bool, ExportError> {
        let root = &self.projection.root.access;
        let access = &selection.access;
        if !is_description(access.route.operation) ||
            root.route.operation != access.route.operation ||
            root.instance.tenant != access.instance.tenant ||
            root.destination != access.destination ||
            root.purpose != access.purpose ||
            owner(&access.route.target) != &access.instance.owner
        {
            return Err(ExportError::Unavailable);
        }
        self.source
            .permitted(access, self.runtime, self.provider)
            .await
    }

    fn spend(&mut self, depth: usize) -> Result<(), ExportError> {
        let maximum = if self.options.profile == SchemaProfile::OpenAiStrict {
            self.options.max_depth.min(8)
        } else {
            self.options.max_depth
        };
        if depth > maximum {
            return Err(ExportError::Limit);
        }
        self.count += 1;
        if self.count > self.options.max_properties {
            return Err(ExportError::Limit);
        }
        Ok(())
    }

    async fn fields(
        &mut self,
        selection: &Selection,
        mut fields: BTreeMap<Name, Field>,
        depth: usize,
    ) -> Result<BTreeMap<String, (Value, bool)>, ExportError> {
        if selection.fields.len() > self.options.max_properties {
            return Err(ExportError::Limit);
        }
        let mut properties = BTreeMap::new();
        let symbol = owner(&selection.access.route.target);
        for name in &selection.fields {
            let access = access_for(
                &selection.access,
                FieldRef::new(symbol.clone(), name.clone()).into(),
                selection.access.route.operation,
            );
            if !self
                .source
                .permitted(&access, self.runtime, self.provider)
                .await?
            {
                continue;
            }
            if let Some(field) = fields.remove(name) {
                let ty = field.kind.value_type()?;
                if let Some(value) = self.value(&ty, depth + 1).await? {
                    properties
                        .insert(name.to_string(), (value, self.optional(&ty)));
                }
            }
        }
        Ok(properties)
    }

    fn object(&self, properties: BTreeMap<String, (Value, bool)>) -> Value {
        let mut required = Vec::new();
        let mut values = Map::new();
        for (name, (value, optional)) in properties {
            if !optional || self.options.profile == SchemaProfile::OpenAiStrict
            {
                required.push(name.clone());
            }
            values.insert(name, value);
        }
        json!({"type": "object", "properties": values, "required": required, "additionalProperties": false})
    }

    fn value<'a>(
        &'a mut self,
        ty: &'a TypeRef,
        depth: usize,
    ) -> Pin<
        Box<
            dyn Future<Output = Result<Option<Value>, ExportError>>
                + Send
                + 'a,
        >,
    > {
        Box::pin(async move {
            self.spend(depth)?;
            let value = match ty {
                TypeRef::Primitive(ty) => {
                    scalar::primitive(*ty, self.options)?
                },
                TypeRef::Option(inner) => {
                    let Some(inner) = self.value(inner, depth + 1).await?
                    else {
                        return Ok(None);
                    };
                    json!({"anyOf": [inner, {"type": "null"}]})
                },
                TypeRef::List {
                    element,
                    max_length,
                } => {
                    let Some(element) = self.value(element, depth + 1).await?
                    else {
                        return Ok(None);
                    };
                    json!({"type": "array", "items": element, "maxItems": max_length.get()})
                },
                TypeRef::External(id) => {
                    let mapping =
                        self.mappings.0.get(id).ok_or(ExportError::Mapping)?;
                    if mapping.precision == MappingPrecision::Rounded &&
                        !self.options.allow_float_rounding
                    {
                        return Err(ExportError::Precision);
                    }
                    scalar::compatible(&mapping.schema, self.options.profile)?;
                    mapping.schema.clone()
                },
                TypeRef::Entity(symbol) => {
                    let Some(selection) =
                        self.projection.definitions.get(symbol)
                    else {
                        return Ok(None);
                    };
                    if selection.access.route.target !=
                        Reference::Symbol(symbol.clone()) ||
                        !self.approved(selection).await?
                    {
                        return Ok(None);
                    }
                    // The target's content is never traversed. The approved
                    // logical identity gives the ID an explicit domain.
                    let identity = format!(
                        "{}::{}@{}",
                        symbol.key().ontology(),
                        symbol.key().name(),
                        symbol.version()
                    );
                    self.object(BTreeMap::from([
                        (
                            "id".into(),
                            (json!({"type": "string", "pattern": "^[\\s\\S]+$"}), false),
                        ),
                        (
                            "type".into(),
                            (
                                json!({"type": "string", "enum": [identity]}),
                                false,
                            ),
                        ),
                    ]))
                },
                TypeRef::Named(symbol) => {
                    let Some(selection) =
                        self.projection.definitions.get(symbol)
                    else {
                        return Ok(None);
                    };
                    if selection.access.route.target !=
                        Reference::Symbol(symbol.clone()) ||
                        !self.approved(selection).await?
                    {
                        return Ok(None);
                    }
                    let key = format!(
                        "{}::{}@{}",
                        symbol.key().ontology(),
                        symbol.key().name(),
                        symbol.version()
                    );
                    if !self.definitions.contains_key(&key) {
                        if !self.active.insert(symbol.clone()) {
                            return Err(ExportError::Unsupported);
                        }
                        let definition =
                            self.source.definition(symbol).await?;
                        let Some(value) = self
                            .nominal(definition, selection, depth + 1)
                            .await?
                        else {
                            self.active.remove(symbol);
                            return Ok(None);
                        };
                        self.definitions.insert(key.clone(), value);
                        self.active.remove(symbol);
                    }
                    json!({"$ref": format!("#/$defs/{}", key.replace('~', "~0").replace('/', "~1"))})
                },
            };
            Ok(Some(value))
        })
    }

    fn nominal<'a>(
        &'a mut self,
        definition: Definition,
        selection: &'a Selection,
        depth: usize,
    ) -> Pin<
        Box<
            dyn Future<Output = Result<Option<Value>, ExportError>>
                + Send
                + 'a,
        >,
    > {
        Box::pin(async move {
            self.spend(depth)?;
            match definition {
                Definition::Entity { fields } |
                Definition::Struct { fields } => {
                    let properties =
                        self.fields(selection, fields, depth).await?;
                    Ok(Some(self.object(properties)))
                },
                Definition::Enum { variants } => {
                    if variants.len() >
                        self.options
                            .max_properties
                            .saturating_sub(self.count)
                    {
                        return Err(ExportError::Limit);
                    }
                    self.count += variants.len();
                    Ok(Some(json!({"type": "string", "enum": variants})))
                },
                Definition::TypeAlias { target } |
                Definition::Attribute { value_type: target } => {
                    let schema = self.value(&target, depth + 1).await?;
                    if schema.is_some() && self.optional(&target) {
                        self.optional_aliases.insert(
                            owner(&selection.access.route.target).clone(),
                        );
                    }
                    Ok(schema)
                },
                _ => Err(ExportError::Unsupported),
            }
        })
    }
}

pub(super) async fn submit(
    source: &impl Source,
    document: &SchemaDocument,
    selection: &Selection,
    proposal: &Value,
    runtime: &(impl TrustedRuntime + Sync),
    provider: &(impl PermissionProvider + Sync),
    business: &(impl super::GolValidator + Sync),
) -> Result<(), ExportError> {
    document.validate_json(proposal)?;
    let properties = proposal.as_object().ok_or(ExportError::InvalidJson)?;
    let Reference::Symbol(symbol) = &selection.access.route.target else {
        return Err(ExportError::Unsupported);
    };
    let Definition::Entity { mut fields } = source.definition(symbol).await?
    else {
        return Err(ExportError::Unsupported);
    };
    let mut requests = vec![access_for(
        &selection.access,
        symbol.clone().into(),
        crate::authorization::Operation::Write,
    )];
    for name in properties.keys() {
        let name: Name = name.parse().map_err(|_| ExportError::InvalidJson)?;
        if !document.emitted.contains(&name) {
            return Err(ExportError::InvalidJson);
        }
        let field = fields.remove(&name).ok_or(ExportError::Unavailable)?;
        assignment_type(source, field.kind.value_type()?, 0).await?;
        requests.push(access_for(
            &selection.access,
            FieldRef::new(symbol.clone(), name).into(),
            crate::authorization::Operation::Write,
        ));
    }
    for access in &requests {
        if !source.permitted(access, runtime, provider).await? {
            return Err(ExportError::WriteDenied);
        }
    }
    let target = requests.first().ok_or(ExportError::Unavailable)?;
    if business.validate(target, proposal).await !=
        super::GolValidation::Accepted
    {
        return Err(ExportError::Business);
    }
    Ok(())
}

/// Rejects record replacement until the runtime can bind writes to every
/// nested protected member. A parent field's grant cannot authorize them.
fn assignment_type<'a>(
    source: &'a impl Source,
    ty: TypeRef,
    depth: usize,
) -> Pin<Box<dyn Future<Output = Result<(), ExportError>> + Send + 'a>> {
    Box::pin(async move {
        if depth > 32 {
            return Err(ExportError::Limit);
        }
        match ty {
            TypeRef::Named(symbol) => {
                match source.definition(&symbol).await? {
                    Definition::TypeAlias { target } |
                    Definition::Attribute { value_type: target } => {
                        assignment_type(source, target, depth + 1).await
                    },
                    Definition::Struct { .. } | Definition::Entity { .. } => {
                        Err(ExportError::WriteDenied)
                    },
                    Definition::Enum { .. } => Ok(()),
                    _ => Err(ExportError::Unsupported),
                }
            },
            TypeRef::Option(inner) | TypeRef::List { element: inner, .. } => {
                assignment_type(source, *inner, depth + 1).await
            },
            TypeRef::Primitive(_) |
            TypeRef::External(_) |
            TypeRef::Entity(_) => Ok(()),
        }
    })
}
