//! Axum HTTP and WebSocket server for GraphQL.

use std::sync::{Arc, OnceLock};
use std::time::Instant;

use axum::Router;
use axum::extract::{Extension, WebSocketUpgrade};
use axum::http::{HeaderMap, Request};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use juniper_axum::extract::JuniperRequest;
use juniper_axum::response::JuniperResponse;
use juniper_axum::subscriptions;
use juniper_graphql_ws::ConnectionConfig;
use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::propagation::Extractor;
use opentelemetry::{KeyValue, global};
use tracing::Instrument as _;
use tracing_opentelemetry::OpenTelemetrySpanExt as _;

use crate::adapters::inbound::graphql::auth::{Claims, JwtRuntime};
use crate::adapters::inbound::graphql::context::AppContext;
use crate::adapters::inbound::graphql::schema::{AppSchema, create_schema};
use crate::adapters::outbound::database::iam::PgIamStore;
use crate::adapters::outbound::storage::s3::S3Uploader;
use crate::application::usecases::authorization::AuthService;
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::application::usecases::search::SearchService;
use crate::config::AppConfig;

struct HttpMetrics {
    requests: Counter<u64>,
    duration: Histogram<f64>,
}

static HTTP_METRICS: OnceLock<HttpMetrics> = OnceLock::new();

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
    config: Arc<AppConfig>,
    jwt: Arc<JwtRuntime>,
    identity: Arc<IdentityService>,
    iam_admin: Arc<IamAdminService>,
    explore: Arc<ExploreService>,
    search: Arc<SearchService>,
    auth_service: Arc<AuthService>,
    iam_store: Arc<PgIamStore>,
    s3: Arc<S3Uploader>,
) -> Router {
    // Register instruments during bootstrap so requests never pay setup costs.
    let _ = http_metrics();
    let schema = Arc::new(create_schema());

    Router::new()
        .route("/graphql", post(graphql_handler))
        .route("/graphql", get(graphql_ws))
        .layer(Extension(schema))
        .layer(Extension(config))
        .layer(Extension(identity))
        .layer(Extension(iam_admin))
        .layer(Extension(explore))
        .layer(Extension(search))
        .layer(Extension(auth_service))
        .layer(Extension(iam_store))
        .layer(Extension(s3))
        .layer(Extension(jwt))
        .layer(middleware::from_fn(trace_context))
}

struct HeaderExtractor<'a>(&'a HeaderMap);

impl Extractor for HeaderExtractor<'_> {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).and_then(|value| value.to_str().ok())
    }

    fn keys(&self) -> Vec<&str> {
        self.0.keys().map(axum::http::HeaderName::as_str).collect()
    }
}

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
    Extension(config): Extension<Arc<AppConfig>>,
    Extension(identity): Extension<Arc<IdentityService>>,
    Extension(iam_admin): Extension<Arc<IamAdminService>>,
    Extension(explore): Extension<Arc<ExploreService>>,
    Extension(search): Extension<Arc<SearchService>>,
    Extension(auth_service): Extension<Arc<AuthService>>,
    Extension(iam_store): Extension<Arc<PgIamStore>>,
    Extension(s3): Extension<Arc<S3Uploader>>,
    claims: Claims,
    JuniperRequest(req): JuniperRequest,
) -> JuniperResponse {
    let context = AppContext {
        user_id: claims.sub,
        tenant_id: claims.tenant_id,
        config,
        identity,
        iam_admin,
        explore,
        search,
        auth_service,
        iam_store,
        s3,
    };

    let response = req.execute(&*schema, &context).await;
    JuniperResponse(response)
}

/// Handles GraphQL WebSocket subscriptions.
async fn graphql_ws(
    Extension(schema): Extension<Arc<AppSchema>>,
    Extension(config): Extension<Arc<AppConfig>>,
    Extension(identity): Extension<Arc<IdentityService>>,
    Extension(iam_admin): Extension<Arc<IamAdminService>>,
    Extension(explore): Extension<Arc<ExploreService>>,
    Extension(search): Extension<Arc<SearchService>>,
    Extension(auth_service): Extension<Arc<AuthService>>,
    Extension(iam_store): Extension<Arc<PgIamStore>>,
    Extension(s3): Extension<Arc<S3Uploader>>,
    ws: WebSocketUpgrade,
) -> impl IntoResponse {
    let context = AppContext {
        user_id: "ws_user".to_string(),
        tenant_id: "ws_tenant".to_string(),
        config,
        identity,
        iam_admin,
        explore,
        search,
        auth_service,
        iam_store,
        s3,
    };

    ws.on_upgrade(|socket| async move {
        let config = ConnectionConfig::new(context);
        subscriptions::serve_ws(socket, schema, config).await
    })
}

#[cfg(test)]
mod tests {
    use opentelemetry::trace::TraceContextExt as _;
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
}
