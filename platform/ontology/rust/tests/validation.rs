//! Portable invariant evaluation cannot infer capabilities from JSON encoding.

use std::collections::BTreeMap;

use galadril_ontology::checked::{BinaryOp, Checked, Literal, Value};
use galadril_ontology::validation::{
    Bindings, Contract, Error, Limits, Scalar,
};
use galadril_ontology::{
    ExternalTypeId, FieldRef, ModuleVersion, Name, Primitive, SymbolRef,
    TypeRef, Version,
};

type TestResult = Result<(), Box<dyn std::error::Error>>;

fn owner(name: &str) -> Result<SymbolRef, Box<dyn std::error::Error>> {
    Ok(
        ModuleVersion::new("test.rules".parse()?, Version::new(1, 0, 0))
            .reference(name.parse()?),
    )
}
fn literal(ty: Primitive, value: Literal) -> Checked {
    Checked {
        ty: ty.into(),
        value: Value::Literal(value),
    }
}
fn binary(
    left: Checked,
    op: BinaryOp,
    right: Checked,
    ty: Primitive,
) -> Checked {
    Checked {
        ty: ty.into(),
        value: Value::Binary(Box::new(left), vec![(op, right)]),
    }
}
fn contract(body: &Checked) -> Result<Contract, Box<dyn std::error::Error>> {
    #[derive(serde::Serialize)]
    struct Predicate<'a> {
        name: &'a str,
        body: &'a Checked,
    }
    #[derive(serde::Serialize)]
    struct Wire<'a> {
        owner: SymbolRef,
        predicates: [Predicate<'a>; 1],
    }
    let bytes = serde_json::to_vec(&Wire {
        owner: owner("Record")?,
        predicates: [Predicate {
            name: "test.rules.Admissible",
            body,
        }],
    })?;
    Ok(Contract::decode(&bytes, Limits::default())?)
}
fn field(path: Vec<FieldRef>, ty: TypeRef) -> Checked {
    Checked {
        ty,
        value: Value::Field { base: None, path },
    }
}

#[derive(Default)]
struct Facts {
    values: BTreeMap<Vec<FieldRef>, i16>,
    context: BTreeMap<Name, bool>,
}
impl Bindings for Facts {
    fn field<'a>(
        &'a self,
        base: Option<&Checked>,
        path: &[FieldRef],
        _expected: &TypeRef,
    ) -> Result<Scalar<'a>, Error> {
        if base.is_some() {
            return Err(Error::Unsupported);
        }
        self.values
            .get(path)
            .copied()
            .map(Scalar::I16)
            .ok_or(Error::MissingBinding)
    }

    fn context<'a>(
        &'a self,
        name: &Name,
        _expected: &TypeRef,
    ) -> Result<Scalar<'a>, Error> {
        self.context
            .get(name)
            .copied()
            .map(Scalar::Bool)
            .ok_or(Error::MissingBinding)
    }
}

#[test]
fn a_false_invariant_is_rejected_and_a_true_one_is_not_an_authorization_grant()
-> TestResult {
    let facts = Facts::default();
    contract(&literal(Primitive::Bool, Literal::Bool(true)))?
        .check(&facts, Limits::default())?;
    assert_eq!(
        contract(&literal(Primitive::Bool, Literal::Bool(false)))?
            .check(&facts, Limits::default()),
        Err(Error::Violation)
    );
    Ok(())
}

#[test]
fn full_paths_distinguish_two_values_of_the_same_struct_type() -> TestResult {
    let record = owner("Record")?;
    let address = owner("Address")?;
    let billing = vec![
        FieldRef::new(record.clone(), "billing".parse()?),
        FieldRef::new(address.clone(), "code".parse()?),
    ];
    let shipping = vec![
        FieldRef::new(record, "shipping".parse()?),
        FieldRef::new(address, "code".parse()?),
    ];
    let mut facts = Facts::default();
    facts.values.insert(billing.clone(), 11);
    facts.values.insert(shipping.clone(), 17);
    let body = binary(
        field(billing, Primitive::I16.into()),
        BinaryOp::NotEqual,
        field(shipping, Primitive::I16.into()),
        Primitive::Bool,
    );
    contract(&body)?.check(&facts, Limits::default())?;
    assert_eq!(
        contract(&body)?.check(&Facts::default(), Limits::default()),
        Err(Error::MissingBinding)
    );
    Ok(())
}

#[test]
fn typed_context_and_all_boolean_operands_must_be_available() -> TestResult {
    let context = Checked {
        ty: Primitive::Bool.into(),
        value: Value::Context("mfa".parse()?),
    };
    let body = binary(
        literal(Primitive::Bool, Literal::Bool(true)),
        BinaryOp::Or,
        context,
        Primitive::Bool,
    );
    assert_eq!(
        contract(&body)?.check(&Facts::default(), Limits::default()),
        Err(Error::MissingBinding)
    );
    let mut facts = Facts::default();
    facts.context.insert("mfa".parse()?, true);
    contract(&body)?.check(&facts, Limits::default())?;
    Ok(())
}

#[test]
fn integer_width_overflow_and_division_by_zero_do_not_wrap_or_panic()
-> TestResult {
    let overflow = binary(
        literal(Primitive::I8, Literal::Integer(127)),
        BinaryOp::Add,
        literal(Primitive::I8, Literal::Integer(1)),
        Primitive::I8,
    );
    let body = binary(
        overflow,
        BinaryOp::Equal,
        literal(Primitive::I8, Literal::NegativeInteger(128)),
        Primitive::Bool,
    );
    assert_eq!(
        contract(&body)?.check(&Facts::default(), Limits::default()),
        Err(Error::Arithmetic)
    );
    let divide = binary(
        literal(Primitive::I64, Literal::Integer(9)),
        BinaryOp::Divide,
        literal(Primitive::I64, Literal::Integer(0)),
        Primitive::I64,
    );
    let body = binary(
        divide,
        BinaryOp::Equal,
        literal(Primitive::I64, Literal::Integer(0)),
        Primitive::Bool,
    );
    assert_eq!(
        contract(&body)?.check(&Facts::default(), Limits::default()),
        Err(Error::Arithmetic)
    );
    let exact = binary(
        literal(Primitive::U128, Literal::Integer(u128::MAX)),
        BinaryOp::Equal,
        literal(Primitive::U128, Literal::Integer(u128::MAX)),
        Primitive::Bool,
    );
    contract(&exact)?.check(&Facts::default(), Limits::default())?;
    Ok(())
}

#[test]
fn bindings_are_not_coerced_to_a_different_rust_primitive() -> TestResult {
    let path = vec![FieldRef::new(owner("Record")?, "count".parse()?)];
    let mut facts = Facts::default();
    facts.values.insert(path.clone(), 5);
    let body = binary(
        field(path, Primitive::I8.into()),
        BinaryOp::Equal,
        literal(Primitive::I8, Literal::Integer(5)),
        Primitive::Bool,
    );
    assert_eq!(
        contract(&body)?.check(&facts, Limits::default()),
        Err(Error::TypeMismatch)
    );
    Ok(())
}

#[test]
fn nonfinite_floats_and_unsupported_opaque_types_fail_closed() -> TestResult {
    let nan = literal(Primitive::F64, Literal::Float(f64::NAN.to_bits()));
    let body = binary(nan.clone(), BinaryOp::Equal, nan, Primitive::Bool);
    assert!(
        contract(&body)?
            .check(&Facts::default(), Limits::default())
            .is_err()
    );
    let external = TypeRef::External(ExternalTypeId::new(
        "gal.time.Instant".parse()?,
        Version::new(1, 0, 0),
    ));
    let opaque = Checked {
        ty: external,
        value: Value::Literal(Literal::String("serialized-time".into())),
    };
    let body =
        binary(opaque.clone(), BinaryOp::Equal, opaque, Primitive::Bool);
    assert_eq!(
        contract(&body)?.check(&Facts::default(), Limits::default()),
        Err(Error::Unsupported)
    );
    let unresolved = Checked {
        ty: Primitive::Bool.into(),
        value: Value::Role {
            relation: owner("Membership")?,
            role: "member".parse()?,
        },
    };
    assert_eq!(
        contract(&unresolved)?.check(&Facts::default(), Limits::default()),
        Err(Error::Unsupported)
    );
    Ok(())
}

struct EnumFacts {
    domain: SymbolRef,
    variant: &'static str,
}
impl Bindings for EnumFacts {
    fn field<'a>(
        &'a self,
        _base: Option<&Checked>,
        _path: &[FieldRef],
        _expected: &TypeRef,
    ) -> Result<Scalar<'a>, Error> {
        Ok(Scalar::Enum {
            owner: &self.domain,
            variant: self.variant,
        })
    }
}
#[test]
fn enum_equality_preserves_nominal_type_and_version() -> TestResult {
    let status = owner("Status")?;
    let value = Checked {
        ty: TypeRef::Named(status.clone()),
        value: Value::Variant(status.clone(), "Received".parse()?),
    };
    let input = field(
        vec![FieldRef::new(owner("Record")?, "status".parse()?)],
        TypeRef::Named(status.clone()),
    );
    let body = binary(input, BinaryOp::Equal, value, Primitive::Bool);
    contract(&body)?.check(
        &EnumFacts {
            domain: status,
            variant: "Received",
        },
        Limits::default(),
    )?;
    assert_eq!(
        contract(&body)?.check(
            &EnumFacts {
                domain: owner("OtherStatus")?,
                variant: "Received"
            },
            Limits::default()
        ),
        Err(Error::TypeMismatch)
    );
    Ok(())
}

struct RoleFacts;
impl Bindings for RoleFacts {
    fn field<'a>(
        &'a self,
        base: Option<&Checked>,
        _path: &[FieldRef],
        _expected: &TypeRef,
    ) -> Result<Scalar<'a>, Error> {
        match base.map(|value| &value.value) {
            Some(Value::Role { role, .. }) if role.as_str() == "sender" => {
                Ok(Scalar::I16(1))
            },
            Some(Value::Role { role, .. }) if role.as_str() == "recipient" => {
                Ok(Scalar::I16(2))
            },
            _ => Err(Error::Unsupported),
        }
    }
}
#[test]
fn a_resolved_path_keeps_its_role_base_instead_of_collapsing_to_the_last_field()
-> TestResult {
    let role_field =
        |role: &str| -> Result<Checked, Box<dyn std::error::Error>> {
            Ok(Checked {
                ty: Primitive::I16.into(),
                value: Value::Field {
                    base: Some(Box::new(Checked {
                        ty: TypeRef::Entity(owner("Person")?),
                        value: Value::Role {
                            relation: owner("Delivery")?,
                            role: role.parse()?,
                        },
                    })),
                    path: vec![FieldRef::new(
                        owner("Person")?,
                        "rank".parse()?,
                    )],
                },
            })
        };
    let body = binary(
        role_field("sender")?,
        BinaryOp::Less,
        role_field("recipient")?,
        Primitive::Bool,
    );
    contract(&body)?.check(&RoleFacts, Limits::default())?;
    assert!(
        contract(&body)?
            .check(&Facts::default(), Limits::default())
            .is_err()
    );
    Ok(())
}

#[test]
fn authenticated_expression_decode_and_execution_are_bounded() -> TestResult {
    let body = literal(Primitive::Bool, Literal::Bool(true));
    let bytes = serde_json::to_vec(
        &serde_json::json!({"owner":owner("Record")?,"predicates":[{"name":"test.rules.Valid","body":body}]}),
    )?;
    let mut limits = Limits::default();
    limits.max_bytes = bytes.len().saturating_sub(1);
    assert!(matches!(
        Contract::decode(&bytes, limits),
        Err(Error::Budget)
    ));
    let mut limits = Limits::default();
    limits.max_steps = 0;
    assert_eq!(
        Contract::decode(&bytes, Limits::default())?
            .check(&Facts::default(), limits),
        Err(Error::Budget)
    );
    let adversarial = format!("{}0{}", "[".repeat(200), "]".repeat(200));
    assert!(matches!(
        Contract::decode(adversarial.as_bytes(), Limits::default()),
        Err(Error::Budget)
    ));
    Ok(())
}

#[test]
fn escaped_unicode_text_is_not_counted_as_json_structure() -> TestResult {
    let text = "\\\"{[λ🦀]}\\";
    let body = binary(
        literal(Primitive::String, Literal::String(text.into())),
        BinaryOp::Equal,
        literal(Primitive::String, Literal::String(text.into())),
        Primitive::Bool,
    );
    contract(&body)?.check(&Facts::default(), Limits::default())?;
    Ok(())
}

#[test]
fn narrow_integer_arithmetic_agrees_with_rust_at_all_left_values_and_boundary_rhs()
-> TestResult {
    let number = |value: i8| {
        literal(
            Primitive::I8,
            if value < 0 {
                Literal::NegativeInteger(i128::from(value).unsigned_abs())
            } else {
                Literal::Integer(i128::from(value).unsigned_abs())
            },
        )
    };
    for left in i8::MIN..=i8::MAX {
        for right in [i8::MIN, -1, 0, 1, i8::MAX] {
            for (operator, expected) in [
                (BinaryOp::Add, left.checked_add(right)),
                (BinaryOp::Divide, left.checked_div(right)),
            ] {
                let result = binary(
                    number(left),
                    operator,
                    number(right),
                    Primitive::I8,
                );
                let body = binary(
                    result,
                    BinaryOp::Equal,
                    number(expected.unwrap_or_default()),
                    Primitive::Bool,
                );
                let actual = contract(&body)?
                    .check(&Facts::default(), Limits::default());
                assert_eq!(
                    actual,
                    expected.map_or(Err(Error::Arithmetic), |_| Ok(())),
                    "{left:?} {operator:?} {right:?}"
                );
            }
        }
    }
    Ok(())
}
