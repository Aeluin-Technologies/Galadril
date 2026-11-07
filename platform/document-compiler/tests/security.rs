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

#[test]
fn plugin_bindings_are_supplied_only_at_render_time() -> anyhow::Result<()> {
    let mut template = template();
    template.scope = Scope::Plugin;
    template.plugin = Some("fixture".into());
    let variable = template
        .variables
        .remove("name")
        .ok_or_else(|| anyhow::anyhow!("Missing fixture variable"))?;
    template.variables.insert("profile_name".into(), variable);
    template.assets.retain(|asset| asset.path != "main.tex");
    template.assets.extend([
        Asset {
            path: "main.tex".into(),
            data: br"\input{information}\input{draft}".to_vec(),
        },
        Asset {
            path: "draft.tex".into(),
            data: b"Default body".to_vec(),
        },
    ]);
    template.assets.push(Asset {
        path: "information.tex".into(),
        data: b"<<galadril:profile_name>>".to_vec(),
    });
    let draft = Document {
        variables: BTreeMap::new(),
        source: Some("Generated body".into()),
    };
    template.validate_submission(&draft)?;
    assert!(template.render(&draft).is_err());
    let forged = Document {
        variables: BTreeMap::from([(
            "profile_name".into(),
            serde_json::json!("forged"),
        )]),
        source: draft.source.clone(),
    };
    assert!(template.validate_submission(&forged).is_err());
    let rendered = template.render_with_bindings(
        &draft,
        &BTreeMap::from([("profile_name".into(), serde_json::json!("A & B"))]),
    )?;
    let asset = rendered
        .assets
        .iter()
        .find(|asset| asset.path == "information.tex")
        .ok_or_else(|| anyhow::anyhow!("Missing information"))?;
    assert_eq!(asset.data, b"A \\& B");
    assert_eq!(rendered.entrypoint, br"\input{information}\input{draft}");
    assert!(
        rendered
            .assets
            .iter()
            .any(|asset| asset.path == "draft.tex" &&
                asset.data == b"Generated body")
    );
    Ok(())
}
