//! Public syntax API conformance and resource-boundary tests.

use std::error::Error;

use galadril_ontology::Primitive;
use galadril_ontology::syntax::{
    AccessMember, ActionClause, Annotation, BinaryOp, Binding, Declaration,
    ErrorCode, Expression, ImportKind, ImportSelection, LabelMember, Literal,
    Member, PolicyKind, Source, Type, parse_source, parse_sources,
};

type TestResult = Result<(), Box<dyn Error>>;

const MAIL: &str = include_str!("fixtures/mail.gol");
const INVALID: &str = include_str!("fixtures/invalid.gol");

#[test]
fn parses_the_worked_mail_profile() -> TestResult {
    let tree = parse_source("mail.gol", MAIL)?;
    let module = &tree.modules.first().ok_or("missing module")?.value;
    assert_eq!(
        module
            .name
            .value
            .segments
            .iter()
            .map(|s| s.value)
            .collect::<Vec<_>>(),
        ["communication", "mail"]
    );
    assert_eq!(module.version.value, "0.3.0");
    let mut declarations = module.declarations.iter();
    let Declaration::Entity(message) =
        &declarations.next().ok_or("missing entity")?.value
    else {
        return Err("expected Message".into());
    };
    assert_eq!(message.name.value, "Message");
    let body = message
        .members
        .iter()
        .find_map(|node| match &node.value {
            Member::Field(field) if field.name.value == "body" => Some(field),
            _ => None,
        })
        .ok_or("missing body")?;
    assert!(matches!(body.ty.value, Type::Primitive(Primitive::String)));
    assert!(matches!(
        body.annotations.first().map(|n| &n.value),
        Some(Annotation::Label(_))
    ));
    assert!(matches!(
        body.annotations.get(1).map(|n| &n.value),
        Some(Annotation::Requires(_))
    ));
    let Member::Field(field) =
        &message.members.get(4).ok_or("missing attachments")?.value
    else {
        return Err("expected attachments field".into());
    };
    let Type::Option(inner) = &field.ty.value else {
        return Err("expected Option".into());
    };
    let Type::List(inner) = &inner.value else {
        return Err("expected List".into());
    };
    let Type::Ref(path) = &inner.value else {
        return Err("expected Ref".into());
    };
    assert_eq!(
        path.value.segments.last().ok_or("missing target")?.value,
        "Attachment"
    );
    assert!(
        matches!(declarations.next().map(|n| &n.value), Some(Declaration::Relation(r)) if r.members.iter().filter(|m| matches!(m.value, Member::Role(_))).count() == 3)
    );
    assert!(
        matches!(declarations.next().map(|n| &n.value), Some(Declaration::Action(a)) if a.clauses.iter().any(|c| matches!(&c.value, ActionClause::Effects(effects) if effects.len() == 3)))
    );
    assert!(
        matches!(declarations.next().map(|n| &n.value), Some(Declaration::Policy(p)) if p.kind.value == PolicyKind::Permit && p.when.is_some() && p.unless.is_some())
    );
    assert!(
        matches!(declarations.next().map(|n| &n.value), Some(Declaration::Policy(p)) if p.kind.value == PolicyKind::Forbid)
    );
    assert!(
        matches!(declarations.next().map(|n| &n.value), Some(Declaration::Import(i)) if i.kind == ImportKind::ExternalType && matches!(&i.selection, ImportSelection::Symbols(symbols) if symbols.len() == 2) && i.version.is_some())
    );
    let access = module
        .declarations
        .iter()
        .find_map(|n| match &n.value {
            Declaration::Access(a) => Some(a),
            _ => None,
        })
        .ok_or("missing access")?;
    let Some(AccessMember::Permission(permission)) =
        access.members.get(1).map(|n| &n.value)
    else {
        return Err("expected permission".into());
    };
    assert!(
        matches!(permission.bindings.first().map(|n| &n.value), Some(Binding::SpiceDb(name)) if name.value == "view")
    );
    assert!(
        matches!(permission.bindings.get(1).map(|n| &n.value), Some(Binding::Cedar(name)) if name.value == r#"Mail::Action::"readBody""#)
    );
    assert!(module.declarations.iter().any(|n| matches!(&n.value, Declaration::Label(label) if label.members.iter().any(|member| matches!(member.value, LabelMember::Classification(_))))));
    assert!(module.declarations.iter().any(
        |n| matches!(&n.value, Declaration::Enum(e) if e.variants.len() == 2)
    ));
    assert!(module.declarations.iter().any(
        |n| matches!(&n.value, Declaration::Struct(s) if s.members.len() == 1)
    ));
    Ok(())
}

#[test]
fn groups_operators_and_preserves_utf8_byte_ranges() -> TestResult {
    let text = r#"ontology a version "1" {
        invariant Check on Unknown { ("clé\t\u{1F512}" != "clé") || !active && total + 2 * 3 >= -4 }
        derive Glyph on Unknown.glyph: char = 'é';
    }"#;
    let tree = parse_source("expression.gol", text)?;
    let declarations = &tree
        .modules
        .first()
        .ok_or("missing module")?
        .value
        .declarations;
    let Some(Declaration::Invariant(invariant)) =
        declarations.first().map(|n| &n.value)
    else {
        return Err("expected invariant".into());
    };
    let Expression::Binary { first, rest } = &invariant.condition.value else {
        return Err("expected disjunction".into());
    };
    assert!(
        matches!(rest.first(), Some(term) if term.operator.value == BinaryOp::Or && matches!(&term.operand.value, Expression::Binary { rest, .. } if rest.first().is_some_and(|term| term.operator.value == BinaryOp::And)))
    );
    let Expression::Grouped(expression) = &first.value else {
        return Err("expected parentheses".into());
    };
    let Expression::Binary { first: lhs, .. } = &expression.value else {
        return Err("expected inequality".into());
    };
    assert!(
        matches!(&lhs.value, Expression::Literal(Literal::String(s)) if s == "clé\t🔒")
    );
    assert_eq!(
        text.get(lhs.span.start..lhs.span.end),
        Some(r#""clé\t\u{1F512}""#)
    );
    let Some(Declaration::Derive(derive)) =
        declarations.get(1).map(|n| &n.value)
    else {
        return Err("expected derivation".into());
    };
    assert_eq!(derive.value.value, Expression::Literal(Literal::Char('é')));
    assert_eq!(
        text.get(derive.value.span.start..derive.value.span.end),
        Some("'é'")
    );
    Ok(())
}

#[test]
fn parses_sources_independently_and_accepts_declaration_permutations()
-> TestResult {
    let declarations = [
        "import missing::{T};",
        "entity A { field peers: List<Ref<other.B>>; }",
        "permit unresolved when { context.mfa };",
    ];
    for order in [
        [0, 1, 2],
        [0, 2, 1],
        [1, 0, 2],
        [1, 2, 0],
        [2, 0, 1],
        [2, 1, 0],
    ] {
        let mut text = String::from(
            "ontology one version \"1.0.0\" { /* { ([ Option< */",
        );
        for index in order {
            text.push_str(declarations.get(index).ok_or("bad test index")?);
        }
        text.push_str("} ontology two version \"1.0.0\" {}");
        let results = parse_sources([
            Source {
                name: "one.gol",
                text: &text,
            },
            Source {
                name: "bad.gol",
                text: INVALID,
            },
            Source {
                name: "three.gol",
                text: "ontology three version \"1\" {}",
            },
        ])
        .collect::<Vec<_>>();
        assert!(
            matches!(results.as_slice(), [Ok(one), Err(bad), Ok(three)] if one.modules.len() == 2 && bad.source_name() == "bad.gol" && three.source.name == "three.gol")
        );
    }
    Ok(())
}

#[test]
fn diagnoses_syntax_with_a_stable_code_and_range() -> TestResult {
    let Err(error) = parse_source("invalid.gol", INVALID) else {
        return Err("accepted missing semicolon".into());
    };
    assert_eq!(error.code(), ErrorCode::Syntax);
    assert_eq!(error.code().as_str(), "gol::syntax");
    assert_eq!(error.source_name(), "invalid.gol");
    assert_eq!(INVALID.get(error.span().start..error.span().end), Some("}"));
    let report = error.with_source(INVALID);
    assert!(report.source_code().is_some());
    assert!(
        report
            .labels()
            .and_then(|mut labels| labels.next())
            .is_some()
    );
    Ok(())
}

#[test]
fn rejects_invalid_shapes_without_resolving_names() -> TestResult {
    for body in [
        "entity A { field x: List<>; }",
        "entity A { field x: Vec<String>; }",
        "entity A { field x: Ref<List<T>>; }",
        "entity A { field x: String @label(); }",
        "enum Payload { Some(String) }",
        "action run() { execute(); }",
        "invariant Call on T { f() }",
        "invariant Comparison on T { a < b < c }",
        "permit Resource when { context.mfa;",
        "permit (principal, action, resource);",
        "import typex::{T};}",
        "entitySpliced {}",
        "attribute A: &str;",
    ] {
        let source = format!("ontology o version \"1\" {{ {body} }}");
        assert!(parse_source("bad.gol", &source).is_err(), "{body}");
    }
    for source in
        ["", "  // comment", "ontology", "ontologyX version \"1\" {}"]
    {
        assert!(parse_source("bad.gol", source).is_err(), "{source:?}");
    }
    let source = r#"ontology o version "\u{D800}" {}"#;
    let Err(error) = parse_source("invalid-unicode.gol", source) else {
        return Err("accepted a surrogate".into());
    };
    assert_eq!(error.code(), ErrorCode::InvalidEscape);
    assert_eq!(
        source.get(error.span().start..error.span().end),
        Some(r"\u{D800}")
    );
    Ok(())
}

#[test]
fn bounds_deep_inputs_and_keeps_long_operator_sequences_flat() -> TestResult {
    let source = format!(
        "ontology o version \"1\" {{ type T = {}A{}; }}",
        "Option<".repeat(5000),
        ">".repeat(5000)
    );
    let Err(error) = parse_source("nested.gol", &source) else {
        return Err("accepted deep type".into());
    };
    assert_eq!(error.code(), ErrorCode::NestingLimit);
    let source = format!(
        "ontology o version \"1\" {{ invariant I on T {{ {}x{} }} }}",
        "(".repeat(1200),
        ")".repeat(1200)
    );
    let Err(error) = parse_source("nested.gol", &source) else {
        return Err("accepted deep expression".into());
    };
    assert_eq!(error.code(), ErrorCode::NestingLimit);
    let oversized = " ".repeat(2 * 1024 * 1024);
    let Err(error) = parse_source("large.gol", &oversized) else {
        return Err("accepted oversized input".into());
    };
    assert_eq!(error.code(), ErrorCode::SourceTooLarge);
    let flat = format!(
        "ontology o version \"1\" {{ invariant I on T {{ {}x }} }}",
        "x + ".repeat(4096)
    );
    let tree = parse_source("flat.gol", &flat)?;
    let declaration = tree
        .modules
        .first()
        .and_then(|m| m.value.declarations.first());
    assert!(
        matches!(declaration.map(|d| &d.value), Some(Declaration::Invariant(i)) if matches!(&i.condition.value, Expression::Binary { rest, .. } if rest.len() == 4096))
    );
    Ok(())
}

#[test]
fn distinguishes_the_depth_and_work_boundaries() -> TestResult {
    let at_boundary = format!(
        "ontology o version \"1\" {{ invariant I on T {{ {}x{} }} }}",
        "(".repeat(30),
        ")".repeat(30),
    );
    assert!(parse_source("at-limit.gol", &at_boundary).is_ok());
    let above_boundary = format!(
        "ontology o version \"1\" {{ invariant I on T {{ {}x{} }} }}",
        "(".repeat(31),
        ")".repeat(31),
    );
    let Err(error) = parse_source("above-limit.gol", &above_boundary) else {
        return Err("accepted one delimiter over the limit".into());
    };
    assert_eq!(error.code(), ErrorCode::NestingLimit);
    assert_eq!(
        above_boundary.get(error.span().start..error.span().end),
        Some("(")
    );
    let flat = format!(
        "ontology o version \"1\" {{ invariant I on T {{ {}x }} }}",
        "x + ".repeat(32_768),
    );
    let Err(error) = parse_source("too-many-tokens.gol", &flat) else {
        return Err("accepted an expression above the token budget".into());
    };
    assert_eq!(error.code(), ErrorCode::TokenLimit);
    Ok(())
}
