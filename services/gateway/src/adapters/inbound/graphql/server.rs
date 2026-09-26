//! Axum HTTP and WebSocket server for GraphQL.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, OnceLock};
use std::time::Instant;

use axum::extract::{DefaultBodyLimit, Extension, WebSocketUpgrade};
use axum::http::{HeaderMap, Request, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use chrono::Timelike as _;
use graphql_parser::query::{
    Definition, OperationDefinition, Selection, SelectionSet, parse_query,
};
use juniper::http::{GraphQLBatchRequest, GraphQLRequest};
use juniper_axum::extract::JuniperRequest;
use juniper_axum::response::JuniperResponse;
use juniper_axum::subscriptions;
use juniper_graphql_ws::ConnectionConfig;
use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::propagation::Extractor;
use opentelemetry::trace::TraceContextExt as _;
use opentelemetry::{KeyValue, global};
use tracing::Instrument as _;
use tracing_opentelemetry::OpenTelemetrySpanExt as _;

use crate::adapters::inbound::graphql::auth::{Claims, JwtRuntime};
use crate::adapters::inbound::graphql::context::AppContext;
use crate::adapters::inbound::graphql::schema::{AppSchema, create_schema};
use crate::application::usecases::authorization::QueryContext;
use crate::application::usecases::control_plane::ControlPlaneService;
use crate::application::usecases::conversations::ConversationService;
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::application::usecases::pipelines::PipelineService;
use crate::application::usecases::search::SearchService;
use crate::application::usecases::uploads::UploadService;
use crate::config::ServerConfig;

#[derive(Clone, Copy)]
struct GraphqlLimits {
    max_body_bytes: usize,
    max_depth: usize,
}

struct HttpMetrics {
    requests: Counter<u64>,
    duration: Histogram<f64>,
}

static HTTP_METRICS: OnceLock<HttpMetrics> = OnceLock::new();

/// Immutable service graph shared by every GraphQL request.
pub struct GatewayServices {
    pub identity: Arc<IdentityService>,
    pub iam_admin: Arc<IamAdminService>,
    pub explore: Arc<ExploreService>,
    pub search: Arc<SearchService>,
    pub control_plane: Arc<ControlPlaneService>,
    pub conversations: Arc<ConversationService>,
    pub pipelines: Arc<PipelineService>,
    pub uploads: Arc<UploadService>,
}

/// Lazily initializes process-wide HTTP metric instruments once.
fn http_metrics() -> &'static HttpMetrics {
    HTTP_METRICS.get_or_init(|| {
        let meter = global::meter("galadril.gateway.http");
        HttpMetrics {
            requests: meter
                .u64_counter("http.server.request.count")
                .with_description("number of inbound http requests")
                .build(),
            duration: meter
                .f64_histogram("http.server.request.duration")
                .with_description("inbound http request duration")
                .with_unit("s")
                .build(),
        }
    })
}

/// Bootstraps the Axum router with pure GraphQL endpoints.
pub fn create_router(
    jwt: Arc<JwtRuntime>,
    services: Arc<GatewayServices>,
    server: &ServerConfig,
) -> Router {
    // Register instruments during bootstrap so requests never pay setup costs.
    http_metrics();
    let schema = Arc::new(create_schema());

    Router::new()
        .route("/graphql", post(graphql_handler))
        .route("/graphql", get(graphql_ws))
        .layer(Extension(schema))
        .layer(Extension(services))
        .layer(Extension(jwt))
        .layer(middleware::from_fn(enforce_content_length))
        .layer(Extension(GraphqlLimits {
            max_body_bytes: server.max_body_bytes,
            max_depth: server.max_graphql_depth,
        }))
        .layer(DefaultBodyLimit::max(server.max_body_bytes))
        .layer(middleware::from_fn(trace_context))
}

/// Rejects declared oversized bodies before allocating a JSON buffer.
async fn enforce_content_length(
    Extension(limits): Extension<GraphqlLimits>,
    request: Request<axum::body::Body>,
    next: Next,
) -> Response {
    let oversized = request
        .headers()
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<usize>().ok())
        .is_some_and(|length| length > limits.max_body_bytes);
    if oversized {
        return (
            StatusCode::PAYLOAD_TOO_LARGE,
            "GraphQL request body exceeds the configured limit",
        )
            .into_response();
    }
    next.run(request).await
}

struct HeaderExtractor<'a>(&'a HeaderMap);

impl Extractor for HeaderExtractor<'_> {
    /// Returns one valid UTF-8 propagation header.
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).and_then(|value| value.to_str().ok())
    }

    /// Returns every header name visible to the OTLP propagator.
    fn keys(&self) -> Vec<&str> {
        self.0.keys().map(axum::http::HeaderName::as_str).collect()
    }
}

/// Extracts distributed trace context and records request latency and status.
async fn trace_context(
    request: Request<axum::body::Body>,
    next: Next,
) -> Response {
    let started_at = Instant::now();
    let parent = global::get_text_map_propagator(|propagator| {
        propagator.extract(&HeaderExtractor(request.headers()))
    });
    let span = tracing::info_span!(
        "http.server.request",
        otel.kind = "server",
        http.request.method = %request.method(),
        url.path = %request.uri().path(),
        http.response.status_code = tracing::field::Empty,
    );
    if let Err(error) = span.set_parent(parent) {
        tracing::warn!(
            event.name = "trace.parent.rejected",
            error = %error,
            "incoming trace parent rejected"
        );
    }

    let response = next.run(request).instrument(span.clone()).await;
    let status_code = response.status().as_u16();
    span.record("http.response.status_code", status_code);
    let attributes = [KeyValue::new(
        "http.response.status_code",
        i64::from(status_code),
    )];
    let metrics = http_metrics();
    metrics.requests.add(1, &attributes);
    metrics
        .duration
        .record(started_at.elapsed().as_secs_f64(), &attributes);
    response
}

/// Handles standard incoming GraphQL queries and mutations.
async fn graphql_handler(
    Extension(schema): Extension<Arc<AppSchema>>,
    Extension(services): Extension<Arc<GatewayServices>>,
    Extension(limits): Extension<GraphqlLimits>,
    claims: Claims,
    JuniperRequest(req): JuniperRequest,
) -> Response {
    if let Err(message) = validate_request_depth(&req, limits.max_depth) {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"errors": [{"message": message}]})),
        )
            .into_response();
    }
    let request_id = uuid::Uuid::new_v4().simple().to_string();
    let authz_context =
        policy_context(&claims, request_id, current_trace_id());
    let authn_issuer = claims.iss.clone();
    let context = AppContext {
        user_id: claims.sub,
        tenant_id: claims.tenant_id,
        authn_issuer,
        authn_expires_at: claims.exp,
        authz_context,
        control_plane: Arc::clone(&services.control_plane),
        conversations: Arc::clone(&services.conversations),
        pipelines: Arc::clone(&services.pipelines),
        identity: Arc::clone(&services.identity),
        iam_admin: Arc::clone(&services.iam_admin),
        explore: Arc::clone(&services.explore),
        search: Arc::clone(&services.search),
        uploads: Arc::clone(&services.uploads),
    };

    let response = req.execute(&*schema, &context).await;
    JuniperResponse(response).into_response()
}

/// Rejects deeply nested operations before resolver execution.
fn validate_request_depth(
    request: &GraphQLBatchRequest,
    max_depth: usize,
) -> Result<(), String> {
    match request {
        GraphQLBatchRequest::Single(request) => {
            validate_operation_depth(request, max_depth)
        },
        GraphQLBatchRequest::Batch(requests) => {
            requests.iter().try_for_each(|request| {
                validate_operation_depth(request, max_depth)
            })
        },
    }
}

/// Expands fragments so aliases cannot bypass the configured depth boundary.
fn validate_operation_depth(
    request: &GraphQLRequest,
    max_depth: usize,
) -> Result<(), String> {
    let document = parse_query::<&str>(&request.query)
        .map_err(|error| format!("Invalid GraphQL document: {error}"))?;
    let fragments = document
        .definitions
        .iter()
        .filter_map(|definition| match definition {
            Definition::Fragment(fragment) => {
                Some((fragment.name, &fragment.selection_set))
            },
            Definition::Operation(_) => None,
        })
        .collect::<HashMap<_, _>>();
    let mut visiting = HashSet::new();
    for definition in &document.definitions {
        if let Definition::Operation(operation) = definition {
            let selections = match operation {
                OperationDefinition::SelectionSet(selection_set) => {
                    selection_set
                },
                OperationDefinition::Query(operation) => {
                    &operation.selection_set
                },
                OperationDefinition::Mutation(operation) => {
                    &operation.selection_set
                },
                OperationDefinition::Subscription(operation) => {
                    &operation.selection_set
                },
            };
            validate_selection_depth(
                selections,
                &fragments,
                &mut visiting,
                1,
                max_depth,
            )?;
        }
    }
    Ok(())
}

/// Traverses one bounded selection tree without executing any resolver.
fn validate_selection_depth<'a>(
    selections: &'a SelectionSet<'a, &'a str>,
    fragments: &HashMap<&'a str, &'a SelectionSet<'a, &'a str>>,
    visiting: &mut HashSet<&'a str>,
    depth: usize,
    max_depth: usize,
) -> Result<(), String> {
    if depth > max_depth {
        return Err(format!(
            "GraphQL selection depth exceeds the configured limit of {max_depth}"
        ));
    }
    for selection in &selections.items {
        match selection {
            Selection::Field(field) => {
                if !field.selection_set.items.is_empty() {
                    validate_selection_depth(
                        &field.selection_set,
                        fragments,
                        visiting,
                        depth + 1,
                        max_depth,
                    )?;
                }
            },
            Selection::InlineFragment(fragment) => {
                validate_selection_depth(
                    &fragment.selection_set,
                    fragments,
                    visiting,
                    depth + 1,
                    max_depth,
                )?;
            },
            Selection::FragmentSpread(spread) => {
                let name = spread.fragment_name;
                if !visiting.insert(name) {
                    return Err(format!(
                        "GraphQL fragment cycle detected at '{name}'"
                    ));
                }
                if let Some(fragment) = fragments.get(name) {
                    validate_selection_depth(
                        fragment,
                        fragments,
                        visiting,
                        depth + 1,
                        max_depth,
                    )?;
                }
                visiting.remove(name);
            },
        }
    }
    Ok(())
}

/// Handles GraphQL WebSocket subscriptions.
async fn graphql_ws(
    Extension(schema): Extension<Arc<AppSchema>>,
    Extension(services): Extension<Arc<GatewayServices>>,
    claims: Claims,
    ws: WebSocketUpgrade,
) -> impl IntoResponse {
    let request_id = uuid::Uuid::new_v4().simple().to_string();
    let authz_context =
        policy_context(&claims, request_id, current_trace_id());
    let authn_issuer = claims.iss.clone();
    let context = AppContext {
        user_id: claims.sub,
        tenant_id: claims.tenant_id,
        authn_issuer,
        authn_expires_at: claims.exp,
        authz_context,
        control_plane: Arc::clone(&services.control_plane),
        conversations: Arc::clone(&services.conversations),
        pipelines: Arc::clone(&services.pipelines),
        identity: Arc::clone(&services.identity),
        iam_admin: Arc::clone(&services.iam_admin),
        explore: Arc::clone(&services.explore),
        search: Arc::clone(&services.search),
        uploads: Arc::clone(&services.uploads),
    };

    ws.on_upgrade(|socket| async move {
        let config = ConnectionConfig::new(context);
        subscriptions::serve_ws(socket, schema, config).await
    })
}

/// Builds trusted Cedar context only from verified claims and runtime facts.
fn policy_context(
    claims: &Claims,
    request_id: String,
    trace_id: Option<String>,
) -> QueryContext {
    QueryContext {
        role: claims.role.clone(),
        region: claims.region.clone(),
        internal_device: claims.device_trust.as_deref() == Some("internal"),
        hour_utc: i64::from(chrono::Utc::now().hour()),
        request_id,
        trace_id,
        ..QueryContext::default()
    }
}

/// Returns the active valid OTLP trace identifier when present.
fn current_trace_id() -> Option<String> {
    let context = tracing::Span::current().context();
    let span = context.span();
    let span_context = span.span_context();
    span_context
        .is_valid()
        .then(|| span_context.trace_id().to_string())
}

#[cfg(test)]
mod tests {
    use juniper::http::{GraphQLBatchRequest, GraphQLRequest};
    use opentelemetry_sdk::propagation::TraceContextPropagator;

    use super::*;

    #[test]
    fn header_extractor_preserves_w3c_remote_parent() {
        global::set_text_map_propagator(TraceContextPropagator::new());
        let mut headers = HeaderMap::new();
        let value = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";
        let parsed = value.parse();
        assert!(parsed.is_ok());
        if let Ok(parsed) = parsed {
            headers.insert("traceparent", parsed);
        }

        let context = global::get_text_map_propagator(|propagator| {
            propagator.extract(&HeaderExtractor(&headers))
        });
        let span = context.span();

        assert!(span.span_context().is_remote());
        assert_eq!(
            span.span_context().trace_id().to_string(),
            "4bf92f3577b34da6a3ce929d0e0e4736"
        );
    }

    #[test]
    fn graphql_depth_limit_expands_fragments() {
        let request = GraphQLBatchRequest::Single(GraphQLRequest::new(
            "query { ontologies { ...Catalog } } fragment Catalog on Ontology { productionPublication { revisionId } }".to_owned(),
            None,
            None,
        ));

        let result = validate_request_depth(&request, 2);

        assert!(matches!(
            result,
            Err(message)
                if message
                    == "GraphQL selection depth exceeds the configured limit of 2"
        ));
    }
}
