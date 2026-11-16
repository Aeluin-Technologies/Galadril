//! End-to-end semantic contracts exercised through the source parser.

use std::error::Error;
use std::num::{NonZeroU32, NonZeroU64};

use galadril_ontology::semantic::{
    Code, Environment, ValidationState, Value, analyze_project,
};
use galadril_ontology::syntax::parse_source;
use galadril_ontology::{
    AdapterContract, Declaration, ExternalTypeDescriptor, ExternalTypeId,
    ExternalTypeRegistry, Primitive, TypeCapability, Version, VersionReq,
    VersionedName,
};

type TestResult = Result<(), Box<dyn Error>>;

fn registry() -> Result<ExternalTypeRegistry, Box<dyn Error>> {
    let mut registry = ExternalTypeRegistry::default();
    registry.register(
        ExternalTypeId::new(
            "gal.time.Instant".parse()?,
            Version::new(1, 0, 0),
        ),
        ExternalTypeDescriptor::new(
            AdapterContract::new(
                VersionedName::new(
                    "adapter.time".parse()?,
                    Version::new(1, 0, 0),
                ),
                VersionReq::STAR,
            ),
            VersionedName::new("wire.instant".parse()?, Version::new(1, 0, 0)),
            NonZeroU64::MIN,
            NonZeroU32::MIN,
        ),
    )?;
    Ok(registry)
}

#[test]
fn checks_literal_bounds_and_keeps_long_property_paths_flat() -> TestResult {
    let registry = ExternalTypeRegistry::default();
    let env = Environment::new(&registry);
    let text = format!(
        "ontology graph version \"1.0.0\" {{
            entity Item {{ field next: Ref<Item>; field mark: i8; }}
            derive Minimum on Item.mark: i8 = -128;
            view Tail from Item {{ project {}mark; }}
        }}",
        "next.".repeat(4096),
    );
    let tree = parse_source("graph.gol", &text)?;
    let project = analyze_project(&[tree], &env)?;
    assert!(project.expressions().values().any(|expression| {
        matches!(&expression.value, Value::Field { path, .. } if path.len() == 4097)
    }));
    let bad = parse_source(
        "bad.gol",
        r#"ontology graph version "1.0.0" {
        entity Item { field mark: i8; }
        derive Minimum on Item.mark: i8 = -129;
    }"#,
    )?;
    let Err(errors) = analyze_project(&[bad], &env) else {
        return Err("accepted an out-of-range signed literal".into());
    };
    assert!(errors.iter().any(|d| d.code() == Code::TypeMismatch));
    Ok(())
}

#[test]
fn disambiguates_module_scopes_and_rejects_unpinned_external_versions()
-> TestResult {
    let mut registry = registry()?;
    let (_, descriptor) =
        registry.iter().next().ok_or("missing descriptor")?;
    let mut descriptor = descriptor.clone();
    descriptor.capabilities.insert(TypeCapability::Equality);
    registry.register(
        ExternalTypeId::new(
            "gal.time.Instant".parse()?,
            Version::new(2, 0, 0),
        ),
        descriptor,
    )?;
    let env = Environment::new(&registry);
    let text = r#"ontology alpha version "1.0.0" { entity Item {} }
    ontology beta version "1.0.0" { entity Item {} }
    ontology consumer version "1.0.0" {
        import alpha as one;
        import beta::Item as Other;
        entity Link { field first: Ref<one.Item>; field second: Ref<Other>; }
    }"#;
    let tree = parse_source("scopes.gol", text)?;
    let good = analyze_project(&[tree], &env)?;
    assert_eq!(good.modules().len(), 3);
    let alpha: galadril_ontology::OntologyId = "alpha".parse()?;
    let beta: galadril_ontology::OntologyId = "beta".parse()?;
    assert_ne!(
        good.modules().get(&alpha).map(|m| m.module()),
        good.modules().get(&beta).map(|m| m.module())
    );
    for (text, code) in [
        (
            r#"ontology alpha version "1.0.0" { entity Item {} }
            ontology beta version "1.0.0" { entity Item {} }
            ontology c version "1.0.0" { import alpha; import beta; }"#,
            Code::AmbiguousImport,
        ),
        (
            r#"ontology alpha version "1.0.0" {}
            ontology c version "1.0.0" { import alpha version "2.0.0"; }"#,
            Code::VersionConflict,
        ),
        (
            r#"ontology c version "1.0.0" { import type gal.time::Instant; }"#,
            Code::AmbiguousImport,
        ),
        (
            r#"ontology c version "1.0.0" { import type gal.time::Missing; }"#,
            Code::UnresolvedImport,
        ),
        (
            r#"ontology a version "1.0.0" { entity Item {} }
            ontology c version "1.0.0" { entity Link { field next: Ref<a.Item>; } }"#,
            Code::InvalidGenericArgument,
        ),
    ] {
        let project = parse_source("invalid.gol", text)?;
        let Err(errors) = analyze_project(&[project], &env) else {
            return Err("expected a resolution error".into());
        };
        assert!(errors.iter().any(|d| d.code() == code), "{errors:?}");
    }
    Ok(())
}

#[test]
fn requires_declared_external_capabilities() -> TestResult {
    let without_equality = registry()?;
    let source = parse_source(
        "compare.gol",
        r#"ontology clock version "1.0.0" {
        import type gal.time::Instant;
        entity Range { field start: Instant; field end: Instant; }
        invariant EqualTime on Range { start == end }
    }"#,
    )?;
    let Err(errors) = analyze_project(
        std::slice::from_ref(&source),
        &Environment::new(&without_equality),
    ) else {
        return Err("opaque external values compared".into());
    };
    assert!(errors.iter().any(|d| d.code() == Code::TypeMismatch));
    let (id, descriptor) = without_equality
        .iter()
        .next()
        .ok_or("missing source descriptor")?;
    let mut descriptor = descriptor.clone();
    descriptor.capabilities.insert(TypeCapability::Equality);
    let mut with_equality = ExternalTypeRegistry::default();
    with_equality.register(id.clone(), descriptor)?;
    assert!(
        analyze_project(&[source], &Environment::new(&with_equality)).is_ok()
    );
    Ok(())
}

#[test]
fn lowers_mail_and_cyclic_imports_to_an_order_independent_project()
-> TestResult {
    let registry = registry()?;
    let mut env = Environment::new(&registry);
    env.context.insert("mfa".parse()?, Primitive::Bool)?;
    let mail =
        parse_source("mail.gol", include_str!("fixtures/semantic_mail.gol"))?;
    let attachments = parse_source(
        "attachments.gol",
        include_str!("fixtures/semantic_attachments.gol"),
    )?;
    let first = analyze_project(&[mail.clone(), attachments.clone()], &env)?;
    let second = analyze_project(&[attachments, mail], &env)?;
    assert_eq!(first, second);
    let module = first
        .modules()
        .get(&"communication.mail".parse()?)
        .ok_or("mail missing")?;
    assert!(
        matches!(module.declarations().get("Delivery"), Some(Declaration::Relation(r)) if r.roles.len() == 3)
    );
    assert!(module.declarations().get("User").is_none());
    assert!(module.declarations().get("Role").is_none());
    assert!(
        first
            .security()
            .values()
            .any(|contract| contract.validation() ==
                ValidationState::PendingExternal)
    );
    assert!(!first.expressions().is_empty());
    assert!(!first.dependencies().is_empty());
    Ok(())
}

#[test]
fn reports_multiple_independent_errors_with_spans() -> TestResult {
    let registry = ExternalTypeRegistry::default();
    let env = Environment::new(&registry);
    let invalid = parse_source(
        "invalid.gol",
        include_str!("fixtures/semantic_invalid.gol"),
    )?;
    let Err(errors) = analyze_project(&[invalid], &env) else {
        return Err("expected errors".into());
    };
    assert!(
        errors.iter().any(
            |d| d.code() == Code::DuplicateSymbol && d.related().is_some()
        )
    );
    assert!(errors.iter().any(|d| d.code() == Code::UnresolvedImport));
    Ok(())
}

#[test]
fn normalizes_independent_declarations_and_checks_semantic_types() -> TestResult
{
    let registry = registry()?;
    let env = Environment::new(&registry);
    let first = parse_source(
        "a.gol",
        "ontology a version \"1.0.0\" { entity T { field v: u64; } invariant I on T { v > 0 } }",
    )?;
    let second = parse_source(
        "b.gol",
        "ontology a version \"1.0.0\" { invariant I on T { v > 0 } entity T { field v: u64; } }",
    )?;
    assert_eq!(
        analyze_project(&[first], &env)?,
        analyze_project(&[second], &env)?
    );
    for (body, code) in [
        ("struct T { field next: List<T>; }", Code::Cycle),
        ("entity T {} invariant I on T { 42 }", Code::TypeMismatch),
        (
            "entity T {} invariant I on T { unknown }",
            Code::InvalidPropertyAccess,
        ),
        ("entity T { field x: Missing; }", Code::UnknownType),
        (
            "entity T { field x: i64; } invariant I on T { x == true }",
            Code::TypeMismatch,
        ),
        (
            "struct T {} relation R { role p: T [1..1]; }",
            Code::InvalidRelationRole,
        ),
        (
            "entity T {} relation R { role p: T [2..1]; }",
            Code::InvalidCardinality,
        ),
        (
            "entity T { field x: Ref<S>; } struct S {}",
            Code::InvalidGenericArgument,
        ),
        ("entity T {} permit T.missing;", Code::InvalidSecurityTarget),
        (
            "entity T { field value: String @label(Undefined); }",
            Code::InvalidSecurityTarget,
        ),
    ] {
        let text = format!("ontology invalid version \"1.0.0\" {{ {body} }}");
        let tree = parse_source("invalid.gol", &text)?;
        let Err(errors) = analyze_project(&[tree], &env) else {
            return Err(format!("accepted {body}").into());
        };
        assert!(
            errors.iter().any(|d| d.code() == code),
            "{body}: {errors:?}"
        );
    }
    Ok(())
}

#[test]
fn detects_namespace_alias_conflicts_even_with_disjoint_exports() -> TestResult
{
    let registry = registry()?;
    let env = Environment::new(&registry);
    let source = parse_source(
        "aliases.gol",
        r#"
        ontology alpha version "1.0.0" { entity First {} }
        ontology beta version "1.0.0" { entity Second {} }
        ontology c version "1.0.0" { import alpha as shared; import beta as shared; }
    "#,
    )?;
    let Err(errors) = analyze_project(&[source], &env) else {
        return Err("two modules shared one namespace alias".into());
    };
    assert!(
        errors
            .iter()
            .any(|error| error.code() == Code::AmbiguousImport &&
                error.related().is_some())
    );
    Ok(())
}

#[test]
fn rejects_invalid_contracts_and_undefined_recursion() -> TestResult {
    let registry = registry()?;
    let env = Environment::new(&registry);
    for (body, expected) in [
        ("type A = B; type B = Option<A>;", Code::Cycle),
        (
            "entity E { field x: bool; field y: bool; } derive X on E.x: bool = y; derive Y on E.y: bool = x;",
            Code::Cycle,
        ),
        ("enum State { Ready, Ready }", Code::DuplicateSymbol),
        (
            "entity E { field x: u64; field x: bool; }",
            Code::DuplicateSymbol,
        ),
        (
            "enum State { Ready } entity E { field state: State; } invariant I on E { state == State::Missing }",
            Code::UnresolvedSymbol,
        ),
        (
            "entity E { field f: f64; } invariant I on E { f > 0.0 }",
            Code::TypeMismatch,
        ),
        (
            "entity E { field x: i8; field y: i16; } invariant I on E { x == y }",
            Code::TypeMismatch,
        ),
        (
            "entity E {} relation R { role x: E [1..1]; role x: E [1..1]; }",
            Code::InvalidRelationRole,
        ),
        (
            "entity E {} relation R { role x: E [1..1]; } invariant I on R { y == y }",
            Code::InvalidPropertyAccess,
        ),
        (
            "entity E { field flag: bool; } relation R { role x: E [1..*]; } invariant I on R { x.flag }",
            Code::UnsupportedSemanticConstruct,
        ),
        (
            "entity E {} action A(e: Ref<E>) effects { read(e.missing) };",
            Code::InvalidPropertyAccess,
        ),
        (
            "entity E { key id: u64; } action A(e: Ref<E>) effects { write(e.id) };",
            Code::InvalidActionContract,
        ),
        (
            "action A(value: bool) retry { deduplicated(other) };",
            Code::InvalidActionContract,
        ),
        (
            "action A() -> bool requires { result };",
            Code::InvalidPropertyAccess,
        ),
        (
            "entity E {} access E { permission read_it { relation: \"view\"; } }",
            Code::InvalidSecurityTarget,
        ),
        ("security label Secret {}", Code::InvalidSecurityTarget),
        (
            "entity E {} permit E when { context.unknown };",
            Code::InvalidPropertyAccess,
        ),
        (
            "entity E {} access E { resource: \"e\"; permission view_it { cedar: \"C::read\"; } field missing requires view_it; }",
            Code::InvalidSecurityTarget,
        ),
        (
            "entity E { field flag: bool @requires(Unknown); }",
            Code::InvalidSecurityTarget,
        ),
    ] {
        let text = format!("ontology example version \"1.0.0\" {{ {body} }}");
        let source = parse_source("invalid.gol", &text)?;
        let Err(errors) = analyze_project(&[source], &env) else {
            return Err(format!("accepted {body}").into());
        };
        assert!(
            errors.iter().any(|error| error.code() == expected),
            "{body}: {errors:?}"
        );
    }
    Ok(())
}

#[test]
fn preserves_security_targets_and_effect_roots_across_permutations()
-> TestResult {
    let registry = registry()?;
    let env = Environment::new(&registry);
    let input = r#"ontology labels version "1.0.0" { security label ConfidentialMail { classification: Confidential; } }
    ontology mail version "1.0.0" {
        import labels::ConfidentialMail as Secret;
        permit Message.body when { true };
        entity Message { field body: String @label(Secret); }
        action Copy(src: Ref<Message>, dest: Ref<Message>) -> bool
            ensures { result }
            effects { read(src.body), read(dest.body) };
        access Message {
            resource: "mail_message";
            permission open_body { relation: "open"; cedar: "Mail::Action::\"Open\""; }
            field body requires open_body;
        }
        forbid Message.open_body when { false };
    }"#;
    let source = parse_source("mail.gol", input)?;
    let mut reversed = source.clone();
    reversed.modules.reverse();
    for module in &mut reversed.modules {
        module.value.declarations.reverse();
    }
    let project = analyze_project(&[source], &env)?;
    assert_eq!(project, analyze_project(&[reversed], &env)?);
    let body = project.security().iter().find_map(|(target, security)| {
        matches!(target, galadril_ontology::Reference::Field(field) if field.name.as_str() == "body").then_some(security)
    }).ok_or("missing field contract")?;
    assert_eq!(
        body.label
            .as_ref()
            .map(|label| label.key().ontology().to_string())
            .as_deref(),
        Some("labels")
    );
    assert_eq!(body.validation(), ValidationState::PendingExternal);
    assert_eq!(body.requires.len(), 1);
    assert_eq!(body.policies.len(), 1);
    let binding = project
        .security()
        .values()
        .find_map(|security| security.permissions.values().next())
        .ok_or("missing binding")?;
    assert_eq!(
        binding
            .spicedb
            .as_ref()
            .map(galadril_ontology::Name::as_str),
        Some("open")
    );
    assert_eq!(binding.cedar.as_deref(), Some(r#"Mail::Action::"Open""#));
    assert!(project.effects().values().any(|effects| effects.len() == 2));
    Ok(())
}

#[test]
fn diagnoses_in_source_order_and_preserves_keys_on_content_changes()
-> TestResult {
    let registry = registry()?;
    let env = Environment::new(&registry);
    let a = parse_source(
        "z.gol",
        r#"ontology a version "1.0.0" { entity Item { field x: Missing; } }"#,
    )?;
    let b = parse_source(
        "a.gol",
        r#"ontology b version "1.0.0" { relation Broken { role participant: Missing [1..1]; } }"#,
    )?;
    let Err(first) = analyze_project(&[a.clone(), b.clone()], &env) else {
        return Err("expected errors".into());
    };
    let Err(second) = analyze_project(&[b, a], &env) else {
        return Err("expected errors".into());
    };
    assert!(first.iter().eq(second.iter()));
    assert_eq!(
        first
            .iter()
            .map(|error| error.location().source.as_ref())
            .collect::<Vec<_>>(),
        ["a.gol", "z.gol"]
    );
    let before = parse_source(
        "before.gol",
        r#"ontology a version "1.0.0" { entity Item {} }"#,
    )?;
    let after = parse_source(
        "after.gol",
        r#"ontology a version "1.1.0" { entity Item { field x: bool; } }"#,
    )?;
    let before = analyze_project(&[before], &env)?;
    let after = analyze_project(&[after], &env)?;
    let old = before
        .symbols()
        .keys()
        .next()
        .ok_or("missing old identity")?;
    let new = after
        .symbols()
        .keys()
        .next()
        .ok_or("missing new identity")?;
    assert_eq!(old.key(), new.key());
    assert_ne!(old.version(), new.version());
    Ok(())
}

#[test]
fn resolves_deep_wrappers_and_registry_order_independently() -> TestResult {
    let one = registry()?;
    let (id, descriptor) = one.iter().next().ok_or("missing external type")?;
    let other =
        ExternalTypeId::new("gal.time.Span".parse()?, Version::new(1, 0, 0));
    let mut first = ExternalTypeRegistry::default();
    first.register(id.clone(), descriptor.clone())?;
    first.register(other.clone(), descriptor.clone())?;
    let mut second = ExternalTypeRegistry::default();
    second.register(other, descriptor.clone())?;
    second.register(id.clone(), descriptor.clone())?;
    let text = format!(
        "ontology a version \"1.0.0\" {{ import type gal.time::{{Instant, Span}}; type Deep = {}List<Instant>{}; }}",
        "Option<".repeat(28),
        ">".repeat(28),
    );
    let source = parse_source("deep.gol", &text)?;
    assert_eq!(
        analyze_project(
            std::slice::from_ref(&source),
            &Environment::new(&first)
        )?,
        analyze_project(&[source], &Environment::new(&second))?,
    );
    Ok(())
}
