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

use crate::adapters::inbound::graphql::auth::Claims;
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
    pub chat_tools: Arc<crate::application::usecases::chat_tools::ChatTools>,
    pub identity: Arc<IdentityService>,
    pub iam_admin: Arc<IamAdminService>,
    pub sso_admin:
        Option<Arc<crate::application::usecases::sso_admin::SsoAdminService>>,
    pub provisioning_key: Option<secrecy::SecretString>,
    pub local_provisioning: bool,
    pub explore: Arc<ExploreService>,
    pub search: Arc<SearchService>,
    pub control_plane: Arc<ControlPlaneService>,
    pub conversations: Arc<ConversationService>,
    pub pengolo: Arc<crate::application::usecases::pengolo::PengoloService>,
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
    services: Arc<GatewayServices>,
    server: &ServerConfig,
) -> Router {
    // Register instruments during bootstrap so requests never pay setup costs.
    http_metrics();
    let schema = Arc::new(create_schema());

    Router::new()
        .route(
            "/internal/chat/tools",
            post(crate::adapters::inbound::chat_tools::invoke),
        )
        .route(
            "/internal/chat/events",
            post(crate::adapters::inbound::pengolo::events),
        )
        .layer(Extension(Arc::clone(&services.pengolo)))
        .layer(Extension(Arc::clone(&services.chat_tools)))
        .route("/healthz", get(|| async { StatusCode::OK }))
        .route(
            "/provisioning/organizations",
            post(crate::adapters::inbound::sso::provision),
        )
        .route(
            "/provisioning/local-organizations",
            post(crate::adapters::inbound::sso::provision_local),
        )
        .route("/pengolo/files/{id}", get(pengolo_file))
        .route("/graphql", post(graphql_handler))
        .route("/graphql", get(graphql_ws))
        .layer(Extension(schema))
        .layer(Extension(services))
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
    let maximum = limits.max_body_bytes;
    let oversized = request
        .headers()
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<usize>().ok())
        .is_some_and(|length| length > maximum);
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
    if let Err(status) = validate_session(&services, &claims).await {
        return status.into_response();
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
        pengolo: Arc::clone(&services.pengolo),
        pipelines: Arc::clone(&services.pipelines),
        identity: Arc::clone(&services.identity),
        iam_admin: Arc::clone(&services.iam_admin),
        sso_admin: services.sso_admin.clone(),
        explore: Arc::clone(&services.explore),
        search: Arc::clone(&services.search),
        uploads: Arc::clone(&services.uploads),
    };

    let mutates = request_mutates(&req);
    let response = req.execute(&*schema, &context).await;
    if mutates {
        services.pengolo.invalidate_identity();
    }
    let mut response = JuniperResponse(response).into_response();
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        axum::http::HeaderValue::from_static("no-store"),
    );
    response
}

fn request_mutates(request: &GraphQLBatchRequest) -> bool {
    let selected = |request: &GraphQLRequest| {
        parse_query::<&str>(&request.query).is_ok_and(|document| document.definitions.iter().any(|definition| {
            matches!(definition, Definition::Operation(OperationDefinition::Mutation(operation)) if request.operation_name.as_deref().is_none_or(|name| operation.name == Some(name)))
        }))
    };
    match request {
        GraphQLBatchRequest::Single(request) => selected(request),
        GraphQLBatchRequest::Batch(requests) => requests.iter().any(selected),
    }
}

#[derive(serde::Deserialize)]
struct FileParameters {
    session: Option<String>,
    #[serde(default)]
    preview: bool,
}

/// Browser downloads recheck live permissions instead of exposing bearer S3
/// URLs.
async fn pengolo_file(
    Extension(services): Extension<Arc<GatewayServices>>,
    claims: Claims,
    axum::extract::Path(id): axum::extract::Path<String>,
    axum::extract::Query(parameters): axum::extract::Query<FileParameters>,
) -> Response {
    if let Err(status) = validate_session(&services, &claims).await {
        return status.into_response();
    }
    let context = policy_context(
        &claims,
        uuid::Uuid::new_v4().simple().to_string(),
        current_trace_id(),
    );
    let actor = crate::application::usecases::pengolo::Principal {
        tenant: &claims.tenant_id,
        user: &claims.sub,
        context: &context,
    };
    match services
        .pengolo
        .file_content(&actor, &id, parameters.session.as_deref())
        .await
    {
        Ok((file, chunks)) => {
            let disposition = if parameters.preview {
                "inline"
            } else {
                "attachment"
            };
            let name = file
                .data
                .get("name")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("document");
            let mime = file
                .data
                .get("mime")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("application/octet-stream");
            let mut response =
                axum::body::Body::from_stream(chunks).into_response();
            let headers = response.headers_mut();
            let content_type = match axum::http::HeaderValue::from_str(mime) {
                Ok(value) => value,
                Err(_) => {
                    return StatusCode::INTERNAL_SERVER_ERROR.into_response();
                },
            };
            let content_disposition = match axum::http::HeaderValue::from_str(
                &format!(
                    "{disposition}; filename*=UTF-8''{}",
                    urlencoding::encode(name)
                ),
            ) {
                Ok(value) => value,
                Err(_) => {
                    return StatusCode::INTERNAL_SERVER_ERROR.into_response();
                },
            };
            headers.insert(header::CONTENT_TYPE, content_type);
            headers.insert(header::CONTENT_DISPOSITION, content_disposition);
            headers.insert(
                header::CACHE_CONTROL,
                axum::http::HeaderValue::from_static("no-store"),
            );
            headers.insert(
                header::X_CONTENT_TYPE_OPTIONS,
                axum::http::HeaderValue::from_static("nosniff"),
            );
            headers.insert(
                "content-security-policy",
                axum::http::HeaderValue::from_static(
                    "default-src 'none'; sandbox",
                ),
            );
            response
        },
        Err(_) => {
            tracing::warn!(event.name = "pengolo.download.refused");
            StatusCode::NOT_FOUND.into_response()
        },
    }
}

/// Rechecks revocable account and passkey state after Envoy validates the JWT.
async fn validate_session(
    services: &GatewayServices,
    claims: &Claims,
) -> Result<(), StatusCode> {
    let Some(service) = &services.sso_admin else {
        return Ok(());
    };
    match service
        .validate_session(
            &claims.tenant_id,
            &claims.sub,
            claims.acr.as_deref(),
            claims.mfa_generation,
        )
        .await
    {
        Ok(true) => Ok(()),
        Ok(false) => {
            tracing::warn!(
                event.name = "authentication.session.revoked",
                "authenticated session rejected"
            );
            Err(StatusCode::UNAUTHORIZED)
        },
        Err(error) => {
            tracing::error!(event.name = "authentication.session.unavailable", error = %error, "session validation unavailable");
            Err(StatusCode::SERVICE_UNAVAILABLE)
        },
    }
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
                    depth,
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
                        fragment, fragments, visiting, depth, max_depth,
                    )?;
                }
                visiting.remove(name);
            },
        }
    }
    Ok(())
}

/// Binds browser negotiation to the protocol Juniper decodes and bounds input
/// frames.
fn configured_websocket(
    ws: WebSocketUpgrade,
    limits: GraphqlLimits,
) -> WebSocketUpgrade {
    ws.protocols(["graphql-transport-ws", "graphql-ws"])
        .max_message_size(limits.max_body_bytes)
        .max_frame_size(limits.max_body_bytes)
}

async fn graphql_ws(
    Extension(schema): Extension<Arc<AppSchema>>,
    Extension(services): Extension<Arc<GatewayServices>>,
    Extension(limits): Extension<GraphqlLimits>,
    claims: Claims,
    ws: WebSocketUpgrade,
) -> Response {
    if let Err(status) = validate_session(&services, &claims).await {
        return status.into_response();
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
        pengolo: Arc::clone(&services.pengolo),
        pipelines: Arc::clone(&services.pipelines),
        identity: Arc::clone(&services.identity),
        iam_admin: Arc::clone(&services.iam_admin),
        sso_admin: services.sso_admin.clone(),
        explore: Arc::clone(&services.explore),
        search: Arc::clone(&services.search),
        uploads: Arc::clone(&services.uploads),
    };

    configured_websocket(ws, limits).on_upgrade(|socket| async move {
        let deadline = match websocket_deadline(
            context.authn_expires_at,
            usize::try_from(chrono::Utc::now().timestamp())
                .unwrap_or(usize::MAX),
            tokio::time::Instant::now(),
        ) {
            Ok(deadline) => deadline,
            Err(error) => {
                tracing::warn!(event.name = "authentication.websocket.rejected", reason = %error,
                    "closing connection with unrepresentable expiry");
                return;
            },
        };
        let config = ConnectionConfig::new(context).with_max_in_flight_operations(16);
        tokio::select! {
            () = subscriptions::serve_ws(socket, schema, config) => {},
            () = tokio::time::sleep_until(deadline) => {
                tracing::info!(event.name = "authentication.websocket.expired",
                    "closing expired authenticated connection");
            },
        }
    }).into_response()
}

/// Prevents an extreme JWT expiry from overflowing the runtime timer.
fn websocket_deadline(
    expires_at: usize,
    epoch_now: usize,
    monotonic_now: tokio::time::Instant,
) -> anyhow::Result<tokio::time::Instant> {
    let remaining = u64::try_from(expires_at.saturating_sub(epoch_now))?;
    monotonic_now
        .checked_add(std::time::Duration::from_secs(remaining))
        .ok_or_else(|| {
            anyhow::anyhow!("Unrepresentable authentication deadline")
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
    #[tokio::test]
    async fn graphql_websocket_negotiates_the_requested_transport_protocol()
    -> anyhow::Result<()> {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let router = Router::new().route(
            "/graphql",
            get(|ws: WebSocketUpgrade| async move {
                configured_websocket(
                    ws,
                    GraphqlLimits {
                        max_body_bytes: 4096,
                        max_depth: 8,
                    },
                )
                .on_upgrade(|_socket| async {})
            }),
        );
        let server =
            tokio::spawn(async move { axum::serve(listener, router).await });
        let response = reqwest::Client::new()
            .get(format!("http://{address}/graphql"))
            .header("Connection", "Upgrade")
            .header("Upgrade", "websocket")
            .header("Sec-WebSocket-Version", "13")
            .header("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
            .header("Sec-WebSocket-Protocol", "graphql-transport-ws")
            .send()
            .await?;
        server.abort();
        match server.await {
            Ok(result) => result?,
            Err(error) if error.is_cancelled() => {},
            Err(error) => return Err(error.into()),
        }
        assert_eq!(response.status(), StatusCode::SWITCHING_PROTOCOLS);
        assert_eq!(
            response
                .headers()
                .get("sec-websocket-protocol")
                .and_then(|value| value.to_str().ok()),
            Some("graphql-transport-ws")
        );
        Ok(())
    }

    #[test]
    fn graphql_fragments_do_not_artificially_increase_field_depth() {
        let request = GraphQLBatchRequest::Single(GraphQLRequest::new("query { ontologies { ...A } } fragment A on Ontology { ... on Ontology { ...B } } fragment B on Ontology { ontologyId }".into(), None, None));
        assert!(validate_request_depth(&request, 2).is_ok());
    }
    #[test]
    fn identity_invalidation_only_tracks_the_selected_mutation() {
        use juniper::http::{GraphQLBatchRequest, GraphQLRequest};
        let query = GraphQLBatchRequest::Single(GraphQLRequest::new(
            "query Read { __typename } mutation Write { __typename }".into(),
            Some("Read".into()),
            None,
        ));
        assert!(!super::request_mutates(&query));
        let mutation = GraphQLBatchRequest::Single(GraphQLRequest::new(
            "query Read { __typename } mutation Write { __typename }".into(),
            Some("Write".into()),
            None,
        ));
        assert!(super::request_mutates(&mutation));
    }
    use galadril_telemetry::{ConfigureTelemetry as _, TelemetryConfig};
    use juniper::http::{GraphQLBatchRequest, GraphQLRequest};
    use opentelemetry_sdk::propagation::TraceContextPropagator;

    use super::*;

    #[test]
    fn websocket_deadline_closes_expired_identities() -> anyhow::Result<()> {
        let now = tokio::time::Instant::now();
        assert_eq!(websocket_deadline(99, 100, now)?, now);
        assert_eq!(websocket_deadline(100, 100, now)?, now);
        let expected = now
            .checked_add(std::time::Duration::from_secs(30))
            .ok_or_else(|| anyhow::anyhow!("test deadline overflow"))?;
        assert_eq!(websocket_deadline(130, 100, now)?, expected);
        Ok(())
    }

    #[cfg(target_pointer_width = "64")]
    #[test]
    fn websocket_deadline_rejects_unrepresentable_expiration() {
        assert!(
            websocket_deadline(usize::MAX, 0, tokio::time::Instant::now())
                .is_err()
        );
    }

    #[tokio::test]
    async fn shared_runtime_exposes_the_gateway_span_context()
    -> anyhow::Result<()> {
        let telemetry = TelemetryConfig::Binary {
            name: "gateway-telemetry-test",
            version: "test",
        }
        .configure()?;
        let span = tracing::error_span!("gateway.test.operation");

        assert!(span.in_scope(current_trace_id).is_some());

        drop(span);
        drop(telemetry);
        Ok(())
    }

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
