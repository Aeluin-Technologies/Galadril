//! Untrusted models cannot select authority, filesystem paths, or TeX
//! controls.
use std::collections::BTreeMap;

use galadril_document_compiler::domain::{
    Asset, Document, Scope, Template, Variable, VariableKind, escape_text,
    validate_path,
};

#[test]
fn paths_cannot_escape_the_virtual_project() {
    for path in [
        "../secret",
        "/etc/passwd",
        "a/../../x",
        "a\\b",
        "a//b",
        "./x",
        "a/%2e%2e/x",
        "a\0b",
        "https://host/file",
        "config.json",
    ] {
        assert!(validate_path(path).is_err(), "{path}");
    }
    for path in [
        "main.tex",
        "recoursadmin.cls",
        "sections/information.tex",
        "logo.png",
    ] {
        assert!(validate_path(path).is_ok(), "{path}");
    }
}
#[test]
fn text_variables_never_become_tex_commands() {
    assert_eq!(
        escape_text("\\input{/etc/passwd}%&#_$^~"),
        "\\textbackslash{}input\\{/etc/passwd\\}\\%\\&\\#\\_\\$\\textasciicircum{}\\textasciitilde{}"
    );
}
fn template() -> Template {
    Template { id: "a".repeat(32), version: String::new(), name: "Letter".into(), description: "Custom class".into(), scope: Scope::Personal, owner: "actor".into(), project: None, plugin: None,
        entrypoint: "main.tex".into(), assets: vec![Asset { path: "main.tex".into(), data: b"\\documentclass{letter}\n\\begin{document}<<galadril:name>>\\end{document}".to_vec() }],
        variables: BTreeMap::from([("name".into(), Variable { kind: VariableKind::Text, required: true, choices: Vec::new() })]) }
}
#[test]
fn typed_variables_reject_missing_unknown_and_invalid_values()
-> anyhow::Result<()> {
    let template = template();
    assert!(
        template
            .render(&Document {
                variables: BTreeMap::new(),
                source: None
            })
            .is_err()
    );
    let mut values = BTreeMap::from([(
        "name".into(),
        serde_json::json!("\\write18{evil}"),
    )]);
    let rendered = template.render(&Document {
        variables: values.clone(),
        source: None,
    })?;
    assert!(!std::str::from_utf8(&rendered.entrypoint)?.contains("\\write18"));
    values.insert("other".into(), serde_json::json!(true));
    assert!(
        template
            .render(&Document {
                variables: values,
                source: None
            })
            .is_err()
    );
    Ok(())
}
#[test]
fn revisions_pin_every_asset_and_cannot_alias_other_scopes()
-> anyhow::Result<()> {
    let mut first = template();
    first.seal()?;
    let mut second = first.clone();
    second.assets.push(Asset {
        path: "custom.cls".into(),
        data: b"class".to_vec(),
    });
    second.seal()?;
    assert_ne!(first.version, second.version);
    second.scope = Scope::Project;
    assert!(second.seal().is_err());
    Ok(())
}
#[test]
fn model_input_is_strict_and_has_no_identity_or_storage_selector() {
    for value in [
        serde_json::json!({"variables":{},"source":null,"tenant":"other"}),
        serde_json::json!({"variables":{},"source":null,"bucket":"secrets"}),
        serde_json::json!({"variables":{},"source":null,"command":"sh"}),
    ] {
        assert!(serde_json::from_value::<Document>(value).is_err());
    }
}

#[test]
fn class_macro_groups_are_not_variable_placeholders() -> anyhow::Result<()> {
    let mut template = template();
    template.variables.clear();
    template.assets = vec![Asset {
        path: "main.tex".into(),
        data: b"\\newcommand{\\sample}[1]{{\\bfseries #1}}".to_vec(),
    }];
    let rendered = template.render(&Document {
        variables: BTreeMap::new(),
        source: None,
    })?;
    assert_eq!(
        rendered.entrypoint,
        template
            .assets
            .first()
            .ok_or_else(|| anyhow::anyhow!("Missing asset"))?
            .data
    );
    Ok(())
}
