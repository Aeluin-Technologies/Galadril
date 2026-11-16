//! Phase 1 guarantees exercised through the public Rust API.

use std::collections::BTreeSet;
use std::error::Error;
use std::num::{NonZeroU32, NonZeroU64};

use galadril_ontology::{
    AccessBinding, Action, AdapterContract, Attribute, Cardinality,
    Classification, ContextPolicy, Declaration, Derivation, Effect, Entity,
    EnumType, Expression, ExternalSchema, ExternalTypeDescriptor,
    ExternalTypeId, ExternalTypeRegistry, Field, FieldKind, FieldRef,
    FlowContract, Idempotence, IfcLabel, Import, ImportSelection, Invariant,
    ModelError, ModuleVersion, Name, Named, Ontology, OntologyId, Predicate,
    Primitive, QualifiedName, Relation, RelationshipPermission, Role,
    SecurityContract, StructType, SymbolId, SymbolKind, TransferConstraint,
    TypeAlias, TypeCapability, TypeRef, TypedExpression, Version, VersionReq,
    VersionedName, View,
};

type TestResult = Result<(), Box<dyn Error>>;

fn module(name: &str, version: &str) -> Result<ModuleVersion, Box<dyn Error>> {
    Ok(ModuleVersion::new(name.parse()?, version.parse()?))
}

#[test]
fn resolves_local_declarations_only_at_the_pinned_version() -> TestResult {
    let module = module("mail.core", "0.3.0")?;
    let reference = module.reference("Message".parse()?);
    let mut ontology = Ontology::new(module);
    ontology
        .declare("Message".parse()?, Declaration::Entity(Entity::default()))?;
    assert_eq!(
        ontology.resolve_local(&reference).map(Declaration::kind),
        Some(SymbolKind::Entity),
    );
    assert!(
        ontology
            .resolve_local(
                &ModuleVersion::new(
                    "mail.core".parse()?,
                    Version::new(0, 4, 0)
                )
                .reference("Message".parse()?),
            )
            .is_none()
    );
    Ok(())
}

#[test]
fn models_nary_relations_and_cyclic_business_references() -> TestResult {
    let module = module("industry.assets", "1.0.0")?;
    let asset = module.reference("Asset".parse()?);
    let owner = module.reference("Owner".parse()?);
    let evidence = module.reference("Evidence".parse()?);
    let serial = module.reference("SerialNumber".parse()?);
    let one = Cardinality::new(1, Some(1))?;
    let mut asset_fields = Named::new();
    asset_fields.insert(
        "owner".parse()?,
        Field::new(FieldKind::Value(TypeRef::Entity(owner.clone()))),
    )?;
    asset_fields.insert(
        "serial".parse()?,
        Field::new(FieldKind::OwnedAttribute {
            attribute: serial,
            cardinality: one,
        }),
    )?;
    let mut owner_fields = Named::new();
    owner_fields.insert(
        "primary_asset".parse()?,
        Field::new(FieldKind::Value(TypeRef::Entity(asset.clone()))),
    )?;
    let mut roles = Named::new();
    roles.insert("asset".parse()?, Role::new(asset.clone(), one))?;
    roles.insert("owner".parse()?, Role::new(owner.clone(), one))?;
    roles.insert(
        "evidence".parse()?,
        Role::new(evidence, Cardinality::new(1, None)?),
    )?;
    let mut ontology = Ontology::new(module);
    ontology.declare(
        "Ownership".parse()?,
        Declaration::Relation(Relation {
            roles,
            ..Relation::default()
        }),
    )?;
    ontology.declare(
        "Asset".parse()?,
        Declaration::Entity(Entity {
            fields: asset_fields,
            ..Entity::default()
        }),
    )?;
    ontology.declare(
        "Owner".parse()?,
        Declaration::Entity(Entity {
            fields: owner_fields,
            ..Entity::default()
        }),
    )?;
    ontology.declare(
        "Evidence".parse()?,
        Declaration::Entity(Entity::default()),
    )?;
    ontology.declare(
        "SerialNumber".parse()?,
        Declaration::Attribute(Attribute::new(Primitive::String.into())),
    )?;
    let mut history_roles = Named::new();
    history_roles.insert("asset".parse()?, Role::new(asset, one))?;
    history_roles.insert("former_owner".parse()?, Role::new(owner, one))?;
    ontology.declare(
        "PreviousOwner".parse()?,
        Declaration::Relation(Relation {
            roles: history_roles,
            ..Relation::default()
        }),
    )?;
    assert_eq!(ontology.declarations().len(), 6);
    assert!(
        matches!(ontology.declarations().get("Ownership"), Some(Declaration::Relation(relation)) if relation.roles.len() == 3)
    );
    assert!(matches!(
        Cardinality::new(2, Some(1)),
        Err(ModelError::InvalidCardinality { min: 2, max: 1 })
    ));
    Ok(())
}

#[test]
fn references_other_modules_and_preserves_import_selection() -> TestResult {
    let geo = module("industry.geo", "1.3.0")?;
    let place = geo.reference("Place".parse()?);
    let mut assets = Ontology::new(module("industry.assets", "1.0.0")?);
    assets.add_import(
        geo.ontology().clone(),
        Import::new(
            geo.version().clone(),
            ImportSelection::Only(BTreeSet::from(["Place".parse()?])),
        ),
    )?;
    let mut fields = Named::new();
    fields.insert(
        "location".parse()?,
        Field::new(FieldKind::Value(TypeRef::Entity(place.clone()))),
    )?;
    assets.declare(
        "Asset".parse()?,
        Declaration::Entity(Entity {
            fields,
            ..Entity::default()
        }),
    )?;
    let mut geo_ontology = Ontology::new(geo);
    geo_ontology
        .declare("Place".parse()?, Declaration::Entity(Entity::default()))?;
    assert_eq!(
        assets
            .imports()
            .get(place.key().ontology())
            .map(|import| &import.version),
        Some(place.version())
    );
    assert!(assets.resolve_local(&place).is_none());
    assert!(geo_ontology.resolve_local(&place).is_some());
    Ok(())
}

#[test]
fn describes_structs_enums_and_composed_value_types() -> TestResult {
    let module = module("industry.assets", "1.0.0")?;
    let status = module.reference("Status".parse()?);
    let mut fields = Named::new();
    fields.insert(
        "history".parse()?,
        Field::new(FieldKind::Value(TypeRef::Option(Box::new(
            TypeRef::List {
                element: Box::new(TypeRef::Named(status.clone())),
                max_length: NonZeroU32::new(32).ok_or("invalid test limit")?,
            },
        )))),
    )?;
    let mut ontology = Ontology::new(module);
    ontology.declare(
        "Status".parse()?,
        Declaration::Enum(EnumType::new(BTreeSet::from([
            "Planned".parse()?,
            "Running".parse()?,
            "Archived".parse()?,
        ]))),
    )?;
    ontology.declare(
        "Summary".parse()?,
        Declaration::Struct(StructType {
            fields,
            ..StructType::default()
        }),
    )?;
    let mut sequence = TypeAlias::new(Primitive::U64.into());
    sequence.security =
        Some(Box::new(SecurityContract::new(Version::new(1, 1, 0))));
    ontology.declare(
        "Sequence".parse()?,
        Declaration::TypeAlias(sequence.clone()),
    )?;
    assert_eq!(
        ontology.declarations().get("Sequence"),
        Some(&Declaration::TypeAlias(sequence))
    );
    assert!(
        matches!(ontology.resolve_local(&status), Some(Declaration::Enum(definition)) if definition.variants.contains("Archived"))
    );
    assert!(
        matches!(ontology.declarations().get("Summary"), Some(Declaration::Struct(definition)) if definition.fields.get("history").is_some())
    );
    Ok(())
}

fn external_descriptor() -> Result<ExternalTypeDescriptor, Box<dyn Error>> {
    let adapter = AdapterContract::new(
        VersionedName::new(
            "adapters.time.jiff".parse()?,
            Version::new(1, 2, 0),
        ),
        VersionReq::parse("^1.2")?,
    );
    Ok(ExternalTypeDescriptor::new(
        adapter,
        VersionedName::new("encoding.rfc3339".parse()?, Version::new(1, 0, 0)),
        NonZeroU64::new(128).ok_or("invalid test limit")?,
        NonZeroU32::new(1).ok_or("invalid test depth")?,
    ))
}

#[test]
fn looks_up_exact_external_types_and_retains_only_declared_capabilities()
-> TestResult {
    let name: QualifiedName = "gal.time.Instant".parse()?;
    let id = ExternalTypeId::new(name.clone(), Version::new(1, 0, 0));
    let other = ExternalTypeId::new(name, Version::new(1, 0, 1));
    let mut descriptor = external_descriptor()?;
    assert!(descriptor.capabilities.is_empty());
    let mut schema = ExternalSchema::new(
        VersionedName::new("schema.json".parse()?, Version::new(1, 0, 0)),
        "instant-schema-v1",
    );
    schema.document = Some(r#"{"type":"string"}"#.into());
    descriptor.schema = Some(schema);
    descriptor.capabilities.insert(TypeCapability::Validate);
    let mut registry = ExternalTypeRegistry::default();
    registry.register(id.clone(), descriptor.clone())?;
    assert_eq!(registry.get(&id), Some(&descriptor));
    assert!(registry.get(&other).is_none());
    assert!(!descriptor.capabilities.contains(&TypeCapability::Ordering));
    assert!(!descriptor.capabilities.contains(&TypeCapability::Indexing));
    assert!(!descriptor.capabilities.contains(&TypeCapability::Equality));
    let external = TypeRef::External(id.clone());
    let mut ontology = Ontology::new(module("mail.core", "0.3.0")?);
    ontology.declare(
        "Timestamp".parse()?,
        Declaration::Attribute(Attribute::new(external)),
    )?;
    assert!(matches!(
        ontology.declarations().get("Timestamp"),
        Some(Declaration::Attribute(attribute)) if attribute.value_type == TypeRef::External(id)
    ));
    Ok(())
}

#[test]
fn rejects_invalid_identifiers_and_versions() -> TestResult {
    for invalid in ["", "1name", "trailing ", "path/name", "a.b", "nul\0"] {
        assert!(matches!(
            invalid.parse::<Name>(),
            Err(ModelError::InvalidName)
        ));
    }
    for invalid in ["", ".mail", "mail.", "mail..core", "mail/secret", " mail"]
    {
        assert!(matches!(
            invalid.parse::<QualifiedName>(),
            Err(ModelError::InvalidQualifiedName)
        ));
        assert!(invalid.parse::<OntologyId>().is_err());
    }
    assert!("1".parse::<Version>().is_err());
    assert_eq!("mail_v2".parse::<Name>()?.as_str(), "mail_v2");
    assert_eq!("mail.core".parse::<QualifiedName>()?.as_str(), "mail.core");
    Ok(())
}

#[test]
fn rejects_duplicates_without_replacing_existing_records() -> TestResult {
    let mut ontology = Ontology::new(module("mail.core", "0.3.0")?);
    ontology
        .declare("Message".parse()?, Declaration::Entity(Entity::default()))?;
    assert!(matches!(
        ontology.declare("Message".parse()?, Declaration::TypeAlias(TypeAlias::new(Primitive::String.into()))),
        Err(ModelError::DuplicateName { name }) if name.as_str() == "Message"
    ));
    assert!(matches!(
        ontology.declarations().get("Message"),
        Some(Declaration::Entity(_))
    ));

    let dependency: OntologyId = "mail.attachments".parse()?;
    let original = Import::new(Version::new(1, 0, 0), ImportSelection::All);
    ontology.add_import(dependency.clone(), original.clone())?;
    assert!(matches!(
        ontology.add_import(dependency.clone(), Import::new(Version::new(2, 0, 0), ImportSelection::All)),
        Err(ModelError::DuplicateImport { ontology }) if ontology == dependency
    ));
    assert_eq!(ontology.imports().get(&dependency), Some(&original));

    let mut members = Named::new();
    members.insert("body".parse()?, Primitive::String)?;
    assert!(members.insert("body".parse()?, Primitive::Bool).is_err());
    assert_eq!(members.get("body"), Some(&Primitive::String));

    let id = ExternalTypeId::new(
        "gal.time.Instant".parse()?,
        Version::new(1, 0, 0),
    );
    let mut registry = ExternalTypeRegistry::default();
    let descriptor = external_descriptor()?;
    registry.register(id.clone(), descriptor.clone())?;
    let mut replacement = external_descriptor()?;
    replacement.capabilities.insert(TypeCapability::Equality);
    assert!(
        matches!(registry.register(id.clone(), replacement), Err(ModelError::DuplicateExternalType { external_type }) if external_type == id)
    );
    assert_eq!(registry.get(&id), Some(&descriptor));
    Ok(())
}

#[test]
fn all_permutations_preserve_model_equality_and_logical_keys() -> TestResult {
    let module = module("industry.assets", "1.0.0")?;
    let asset = module.reference("Asset".parse()?);
    let permutations = [
        ["Asset", "Owner", "Site"],
        ["Asset", "Site", "Owner"],
        ["Owner", "Asset", "Site"],
        ["Owner", "Site", "Asset"],
        ["Site", "Asset", "Owner"],
        ["Site", "Owner", "Asset"],
    ];
    let build = |names: [&str; 3],
                 imports: [&str; 2]|
     -> Result<Ontology, ModelError> {
        let mut ontology = Ontology::new(module.clone());
        for name in names {
            ontology.declare(
                name.parse()?,
                Declaration::Entity(Entity::default()),
            )?;
        }
        for name in imports {
            ontology.add_import(
                name.parse()?,
                Import::new(Version::new(1, 0, 0), ImportSelection::All),
            )?;
        }
        Ok(ontology)
    };
    let expected = build(["Asset", "Owner", "Site"], ["geo", "time"])?;
    for order in permutations {
        let ontology = build(order, ["time", "geo"])?;
        assert_eq!(ontology, expected, "declaration order {order:?}");
        assert_eq!(ontology.module().reference("Asset".parse()?), asset);
        assert_eq!(
            ontology
                .declarations()
                .iter()
                .map(|(name, _)| name.as_str())
                .collect::<Vec<_>>(),
            ["Asset", "Owner", "Site"],
        );
    }
    let upgraded =
        ModuleVersion::new(module.ontology().clone(), Version::new(2, 0, 0))
            .reference("Asset".parse()?);
    assert_eq!(asset.key(), upgraded.key());
    assert_ne!(asset, upgraded);
    assert_ne!(SymbolId::from(0), SymbolId::from(1));
    Ok(())
}

#[test]
fn security_contracts_reference_external_policies_without_domain_principals()
-> TestResult {
    let module = module("mail.core", "0.3.0")?;
    let label = module.reference("MailSecret".parse()?);
    let policy = ContextPolicy::new(
        VersionedName::new("mail.policy".parse()?, Version::new(2, 0, 0)),
        r#"Mail::Action::"readBody""#,
    );
    let permission = RelationshipPermission::new(
        VersionedName::new("mail.rebac".parse()?, Version::new(1, 0, 0)),
        VersionedName::new("mail.bridge".parse()?, Version::new(1, 0, 1)),
        "mail_message",
        "view".parse()?,
    );
    let mut operations = Named::new();
    operations.insert(
        "read_body".parse()?,
        AccessBinding::new(permission, policy),
    )?;
    let mut contract = SecurityContract::new(Version::new(1, 1, 0));
    contract.operations = operations;
    contract.flow = Some(FlowContract::new(
        label,
        TransferConstraint::Restricted {
            destinations: BTreeSet::from(["mail.internal".parse()?]),
            purposes: BTreeSet::from(["mail.review".parse()?]),
        },
    ));
    let mut body = Field::new(FieldKind::Value(Primitive::String.into()));
    body.security = Some(Box::new(contract.clone()));
    let mut fields = Named::new();
    fields.insert("body".parse()?, body)?;
    let mut ontology = Ontology::new(module);
    ontology.declare(
        "Message".parse()?,
        Declaration::Entity(Entity {
            fields,
            security: Some(Box::new(contract.clone())),
        }),
    )?;
    ontology.declare(
        "MailSecret".parse()?,
        Declaration::IfcLabel(IfcLabel::new(Classification::Confidential)),
    )?;
    let body_contract = match ontology.declarations().get("Message") {
        Some(Declaration::Entity(entity)) => {
            assert_eq!(entity.security.as_deref(), Some(&contract));
            entity
                .fields
                .get("body")
                .and_then(|field| field.security.as_deref())
        },
        _ => None,
    };
    assert_eq!(body_contract, Some(&contract));
    assert_eq!(ontology.declarations().len(), 2);
    assert!(ontology.declarations().get("User").is_none());
    assert!(ontology.declarations().get("Role").is_none());
    Ok(())
}

#[test]
fn represents_declarative_operation_contracts() -> TestResult {
    let module = module("mail.core", "0.3.0")?;
    let message = module.reference("Message".parse()?);
    let body = FieldRef::new(message.clone(), "body".parse()?);
    let preview = FieldRef::new(message.clone(), "preview".parse()?);
    let mut body_exists = Expression::new("mail.body_exists".parse()?);
    body_exists.dependencies.insert(body.clone().into());
    let predicate = Predicate::new(body_exists);
    let mut make_preview = Expression::new("mail.make_preview".parse()?);
    make_preview.dependencies.insert(body.clone().into());
    let derived = TypedExpression::new(Primitive::String.into(), make_preview);
    let mut action = Action::new(Primitive::Unit.into());
    action
        .parameters
        .insert("message".parse()?, TypeRef::Entity(message.clone()))?;
    action.requires.insert(predicate.clone());
    let mut preview_exists = Expression::new("mail.preview_exists".parse()?);
    preview_exists.dependencies.insert(preview.clone().into());
    action.ensures.insert(Predicate::new(preview_exists));
    action.effects.insert(Effect::Read(body.into()));
    action.effects.insert(Effect::Write(preview.clone().into()));
    action.retry = Idempotence::Deduplicated {
        key_parameter: "message".parse()?,
    };
    let mut view = View {
        filter: Some(predicate.clone()),
        ..View::default()
    };
    view.outputs.insert("preview".parse()?, derived.clone())?;
    let mut fields = Named::new();
    fields.insert(
        "body".parse()?,
        Field::new(FieldKind::Value(Primitive::String.into())),
    )?;
    fields.insert(
        "preview".parse()?,
        Field::new(FieldKind::Value(Primitive::String.into())),
    )?;
    let mut ontology = Ontology::new(module);
    ontology.declare(
        "Message".parse()?,
        Declaration::Entity(Entity {
            fields,
            ..Entity::default()
        }),
    )?;
    ontology.declare(
        "HasBody".parse()?,
        Declaration::Invariant(Invariant::new(message, predicate)),
    )?;
    ontology.declare(
        "BodyPreview".parse()?,
        Declaration::Derive(Derivation::new(preview, derived)),
    )?;
    ontology.declare("Preview".parse()?, Declaration::View(view))?;
    ontology.declare(
        "GeneratePreview".parse()?,
        Declaration::Action(action.clone()),
    )?;
    assert_eq!(
        ontology.declarations().get("GeneratePreview"),
        Some(&Declaration::Action(action))
    );
    assert_eq!(ontology.declarations().len(), 5);
    Ok(())
}
