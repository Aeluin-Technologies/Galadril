//! A protected source must never raise the declared label of a write target.

use galadril_ontology::authorization::{
    Operation, Route, SecurityEnvironment,
};
use galadril_ontology::semantic::{Environment, Project, analyze_project};
use galadril_ontology::syntax::parse_source;
use galadril_ontology::{ExternalTypeRegistry, FieldRef, Primitive};

use super::fixture;
use crate::base;

pub fn project() -> Result<Project, base::TestError> {
    project_with_effects("read(record.z_secret), write(record.a_public)")
}

pub fn project_with_effects(
    effects: &str,
) -> Result<Project, base::TestError> {
    let registry = ExternalTypeRegistry::default();
    let mut environment = Environment::new(&registry);
    for (name, ty) in [
        ("mfa", Primitive::Bool),
        ("hour", Primitive::I64),
        ("region", Primitive::String),
    ] {
        environment.context.insert(name.parse()?, ty)?;
    }
    let source = include_str!("../fixtures/ifc.gol")
        .replace("read(record.z_secret), write(record.a_public)", effects);
    Ok(analyze_project(
        &[parse_source("ifc.gol", &source)?],
        &environment,
    )?)
}

pub fn environment(
    project: &Project,
) -> Result<SecurityEnvironment, base::TestError> {
    let mut environment = fixture::environment(project)?;
    let profile = base::symbol(project, "Profile")?;
    environment.routes.insert(
        Route::new(profile.clone(), Operation::Read),
        ["read_meta".parse()?].into(),
    );
    for name in ["a_public", "z_secret", "restricted"] {
        environment.routes.insert(
            Route::new(
                FieldRef::new(profile.clone(), name.parse()?),
                Operation::Read,
            ),
            Default::default(),
        );
    }
    environment.routes.insert(
        Route::new(base::symbol(project, "Validate")?, Operation::Execute),
        ["read_meta".parse()?].into(),
    );
    Ok(environment)
}
