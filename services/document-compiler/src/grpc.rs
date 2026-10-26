//! Private Tonic boundary bounds payloads and never reflects upstream errors.
use std::sync::Arc;

use galadril_document_compiler::delegation::Action;
use galadril_document_compiler::domain::MAX_PROJECT_BYTES;
use galadril_document_compiler::proto::document_compiler_server::DocumentCompiler;
use galadril_document_compiler::proto::{self};
use tonic::{Request, Response, Status};

use crate::application::Compiler;

pub struct CompilerGrpc(pub Arc<Compiler>);
fn token<T>(request: &Request<T>) -> Result<String, Status> {
    let mut values = request.metadata().get_all("authorization").iter();
    let token = values
        .next()
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .ok_or_else(|| Status::unauthenticated("Delegation required"))?;
    if values.next().is_some() ||
        token.len() != 64 ||
        !token.bytes().all(|c| c.is_ascii_hexdigit())
    {
        return Err(Status::unauthenticated("Invalid delegation"));
    }
    Ok(token.to_owned())
}
fn denied(_: anyhow::Error) -> Status {
    tracing::warn!(event.name = "document_compiler.request.denied");
    Status::permission_denied("Operation denied or unavailable")
}

#[tonic::async_trait]
impl DocumentCompiler for CompilerGrpc {
    async fn list_templates(
        &self,
        request: Request<proto::ListTemplatesRequest>,
    ) -> Result<Response<proto::TemplatePage>, Status> {
        let capability = token(&request)?;
        let (templates, after) = self
            .0
            .list_templates(&capability, &request.into_inner().after)
            .await
            .map_err(denied)?;
        let templates = templates
            .into_iter()
            .map(|template| {
                serde_json::to_vec(&template).map(|manifest_json| {
                    proto::TemplateArtifact { manifest_json }
                })
            })
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| Status::internal("Serialization failed"))?;
        Ok(Response::new(proto::TemplatePage { templates, after }))
    }

    async fn read_template(
        &self,
        request: Request<proto::TemplateRef>,
    ) -> Result<Response<proto::TemplateArtifact>, Status> {
        let capability = token(&request)?;
        let template = self
            .0
            .read_template(&capability, request.into_inner())
            .await
            .map_err(denied)?;
        Ok(Response::new(proto::TemplateArtifact {
            manifest_json: serde_json::to_vec(&template)
                .map_err(|_| Status::internal("Serialization failed"))?,
        }))
    }

    async fn put_template(
        &self,
        request: Request<proto::TemplateArtifact>,
    ) -> Result<Response<proto::TemplateRef>, Status> {
        let capability = token(&request)?;
        let payload = request.into_inner().manifest_json;
        if payload.len() > MAX_PROJECT_BYTES * 5 {
            return Err(Status::resource_exhausted("Template exceeds limit"));
        }
        let template = serde_json::from_slice(&payload)
            .map_err(|_| Status::invalid_argument("Invalid template"))?;
        Ok(Response::new(
            self.0
                .put_template(&capability, template)
                .await
                .map_err(denied)?,
        ))
    }

    async fn publish_template(
        &self,
        request: Request<proto::TemplateRef>,
    ) -> Result<Response<proto::Empty>, Status> {
        let capability = token(&request)?;
        self.0
            .mutate_template(
                &capability,
                request.into_inner(),
                Action::Publish,
            )
            .await
            .map_err(denied)?;
        Ok(Response::new(proto::Empty {}))
    }

    async fn delete_template(
        &self,
        request: Request<proto::TemplateRef>,
    ) -> Result<Response<proto::Empty>, Status> {
        let capability = token(&request)?;
        self.0
            .mutate_template(&capability, request.into_inner(), Action::Delete)
            .await
            .map_err(denied)?;
        Ok(Response::new(proto::Empty {}))
    }

    async fn submit(
        &self,
        request: Request<proto::CompileRequest>,
    ) -> Result<Response<proto::Job>, Status> {
        let capability = token(&request)?;
        Ok(Response::new(
            self.0
                .submit(&capability, request.into_inner())
                .await
                .map_err(denied)?,
        ))
    }

    async fn get_job(
        &self,
        request: Request<proto::JobRef>,
    ) -> Result<Response<proto::Job>, Status> {
        let capability = token(&request)?;
        Ok(Response::new(
            self.0
                .get_job(&capability, &request.into_inner().job_id, false)
                .await
                .map_err(denied)?,
        ))
    }

    async fn cancel(
        &self,
        request: Request<proto::JobRef>,
    ) -> Result<Response<proto::Job>, Status> {
        let capability = token(&request)?;
        Ok(Response::new(
            self.0
                .get_job(&capability, &request.into_inner().job_id, true)
                .await
                .map_err(denied)?,
        ))
    }

    async fn read_pdf(
        &self,
        request: Request<proto::JobRef>,
    ) -> Result<Response<proto::Pdf>, Status> {
        let capability = token(&request)?;
        Ok(Response::new(proto::Pdf {
            content: self
                .0
                .pdf(&capability, &request.into_inner().job_id)
                .await
                .map_err(denied)?,
        }))
    }
}
