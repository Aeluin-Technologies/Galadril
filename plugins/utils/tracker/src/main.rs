//! Public opening signals have no mailbox, database, or sending authority.
#![deny(unsafe_code)]

mod policy;

use std::env;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context as _, Result, bail};
use axum::Router;
use axum::body::Body;
use axum::extract::{ConnectInfo, State};
use axum::http::{Request, StatusCode, header};
use axum::response::Response;
use axum::routing::get;
use galadril_telemetry::{ConfigureTelemetry as _, TelemetryConfig};
use opentelemetry::KeyValue;
use opentelemetry::metrics::Counter;
use reqwest::{Client, Url};
use secrecy::{ExposeSecret as _, SecretString};
use serde::Serialize;
use tokio::net::TcpListener;
use tokio::sync::Semaphore;
use tracing::Instrument as _;

const PIXEL: &[u8] = b"GIF89a\x01\0\x01\0\x80\0\0\0\0\0\xff\xff\xff!\xf9\x04\x01\0\0\0\0,\0\0\0\0\x01\0\x01\0\0\x02\x02D\x01\0;";

struct Collector {
    http: Client,
    sink: Url,
    key: SecretString,
    cloudflare: bool,
    capacity: Arc<Semaphore>,
    outcomes: Counter<u64>,
}

#[derive(Serialize)]
struct Signal<'a> {
    token: &'a str,
    ip: std::net::IpAddr,
}

fn router(collector: Arc<Collector>) -> Router {
    Router::new()
        .route("/healthz", get(|| async { StatusCode::OK }))
        .route("/email/open/{filename}", get(opened))
        .with_state(collector)
}

async fn opened(
    State(collector): State<Arc<Collector>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    request: Request<Body>,
) -> Response {
    if request.method() == axum::http::Method::GET &&
        request.uri().query().is_none() &&
        let Some((token, ip)) = policy::capture(
            request.uri().path(),
            peer.ip(),
            collector.cloudflare,
            request.headers(),
        )
    {
        match Arc::clone(&collector.capacity).try_acquire_owned() {
            Ok(permit) => {
                tokio::spawn(
                    async move {
                        let outcome = match std::str::from_utf8(&token) {
                            Ok(token) => match collector
                                .http
                                .post(collector.sink.clone())
                                .header(
                                    "x-email-tracking-key",
                                    collector.key.expose_secret(),
                                )
                                .json(&Signal { token, ip })
                                .send()
                                .await
                            {
                                Ok(response)
                                    if response.status().is_success() =>
                                {
                                    "recorded"
                                },
                                _ => "unavailable",
                            },
                            Err(_) => "invalid",
                        };
                        collector
                            .outcomes
                            .add(1, &[KeyValue::new("outcome", outcome)]);
                        drop(permit);
                    }
                    .instrument(tracing::info_span!("utils.tracker.capture")),
                );
            },
            Err(_) => collector
                .outcomes
                .add(1, &[KeyValue::new("outcome", "overloaded")]),
        }
    }
    let mut response = Response::new(Body::from(PIXEL));
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("image/gif"),
    );
    headers.insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("no-store, no-cache, max-age=0"),
    );
    headers
        .insert(header::PRAGMA, header::HeaderValue::from_static("no-cache"));
    headers.insert(
        "x-content-type-options",
        header::HeaderValue::from_static("nosniff"),
    );
    response
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let address: SocketAddr = env::var("UTILS_TRACKER_BIND")
        .unwrap_or_else(|_| "127.0.0.1:8096".into())
        .parse()
        .context("Invalid tracker bind")?;
    if env::args().any(|value| value == "--healthcheck") {
        Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(2))
            .build()?
            .get(format!("http://{address}/healthz"))
            .send()
            .await?
            .error_for_status()?;
        return Ok(());
    }
    let cloudflare = match env::var("UTILS_TRACKER_IP_SOURCE").as_deref() {
        Ok("cloudflare") => true,
        Ok("peer") | Err(_) => false,
        _ => bail!("Invalid tracker IP source"),
    };
    if cloudflare && !address.ip().is_loopback() {
        bail!("Cloudflare origin must bind to loopback");
    }
    let sink = Url::parse(&env::var("UTILS_TRACKER_SINK")?)
        .context("Invalid capture sink")?;
    if !matches!(sink.scheme(), "http" | "https") ||
        sink.host_str().is_none() ||
        !sink.username().is_empty() ||
        sink.password().is_some() ||
        sink.path() != "/tracking/open" ||
        sink.query().is_some() ||
        sink.fragment().is_some()
    {
        bail!(
            "Capture sink must be a private HTTP endpoint without credentials or query parameters"
        );
    }
    let key = SecretString::from(
        tokio::fs::read_to_string(env::var("UTILS_TRACKING_KEY_FILE")?)
            .await?
            .trim()
            .to_owned(),
    );
    if key.expose_secret().len() < 32 {
        bail!("Capture requires a private key");
    }
    let telemetry = TelemetryConfig::Binary {
        name: "utils-email-tracker",
        version: env!("CARGO_PKG_VERSION"),
    }
    .configure()?;
    let collector = Arc::new(Collector {
        http: Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(1))
            .timeout(Duration::from_secs(3))
            .pool_max_idle_per_host(4)
            .build()?,
        sink,
        key,
        cloudflare,
        capacity: Arc::new(Semaphore::new(32)),
        outcomes: telemetry
            .meter()
            .u64_counter("utils.email.opening_capture")
            .build(),
    });
    let listener = TcpListener::bind(address)
        .await
        .context("Tracker listener unavailable")?;
    tracing::info!(event = "utils.tracker.started");
    let result = axum::serve(
        listener,
        router(Arc::clone(&collector))
            .into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown())
    .await;
    match tokio::time::timeout(
        Duration::from_secs(4),
        collector.capacity.acquire_many(32),
    )
    .await
    {
        Ok(Ok(permit)) => drop(permit),
        _ => tracing::warn!(event = "utils.tracker.capture_drain_incomplete"),
    }
    telemetry.shutdown()?;
    result.context("Tracker server stopped")
}

async fn shutdown() {
    match tokio::signal::unix::signal(
        tokio::signal::unix::SignalKind::terminate(),
    ) {
        Ok(mut terminate) => tokio::select! {
            result = tokio::signal::ctrl_c() => {
                if result.is_err() { tracing::warn!(event = "utils.tracker.signal_unavailable"); }
            }
            _signal = terminate.recv() => {}
        },
        Err(_) => {
            if tokio::signal::ctrl_c().await.is_err() {
                tracing::warn!(event = "utils.tracker.signal_unavailable");
            }
        },
    }
}

#[cfg(test)]
mod tests {
    use tower::ServiceExt as _;

    use super::*;

    #[tokio::test]
    async fn pixel_is_constant_and_public_routes_cannot_access_mailboxes()
    -> Result<()> {
        let collector = Arc::new(Collector {
            http: Client::new(),
            sink: Url::parse("http://127.0.0.1:1/tracking/open")?,
            key: SecretString::from("k".repeat(40)),
            cloudflare: false,
            capacity: Arc::new(Semaphore::new(0)),
            outcomes: opentelemetry::global::meter("test")
                .u64_counter("capture")
                .build(),
        });
        for path in [
            "/email/open/invalid.gif".to_owned(),
            format!("/email/open/{}.gif", "a".repeat(64)),
        ] {
            let mut request =
                Request::builder().uri(path).body(Body::empty())?;
            request
                .extensions_mut()
                .insert(ConnectInfo("127.0.0.1:1234".parse::<SocketAddr>()?));
            let response =
                router(Arc::clone(&collector)).oneshot(request).await?;
            assert_eq!(response.status(), StatusCode::OK);
            assert_eq!(
                response
                    .headers()
                    .get(header::CACHE_CONTROL)
                    .and_then(|v| v.to_str().ok()),
                Some("no-store, no-cache, max-age=0")
            );
            assert_eq!(
                axum::body::to_bytes(response.into_body(), 1024)
                    .await?
                    .as_ref(),
                PIXEL
            );
        }
        for path in ["/operation", "/tracking/open", "/graphql"] {
            let response = router(Arc::clone(&collector))
                .oneshot(Request::builder().uri(path).body(Body::empty())?)
                .await?;
            assert_eq!(response.status(), StatusCode::NOT_FOUND);
        }
        Ok(())
    }
    #[tokio::test]
    async fn capture_forwards_only_the_opaque_token_and_verified_ip()
    -> Result<()> {
        let (sender, mut receiver) = tokio::sync::mpsc::channel(1);
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let sink = Router::new().route("/tracking/open", axum::routing::post(move |headers: axum::http::HeaderMap, axum::Json(body): axum::Json<serde_json::Value>| {
            let sender = sender.clone();
            async move {
                sender.send((headers, body)).await.map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
                Ok::<_, StatusCode>(StatusCode::OK)
            }
        }));
        let server =
            tokio::spawn(async move { axum::serve(listener, sink).await });
        let collector = Arc::new(Collector {
            http: Client::builder()
                .no_proxy()
                .timeout(Duration::from_secs(2))
                .build()?,
            sink: Url::parse(&format!("http://{address}/tracking/open"))?,
            key: SecretString::from("capture-only".repeat(4)),
            cloudflare: true,
            capacity: Arc::new(Semaphore::new(32)),
            outcomes: opentelemetry::global::meter("test")
                .u64_counter("capture")
                .build(),
        });
        let token = "a".repeat(64);
        let mut request = Request::builder()
            .uri(format!("/email/open/{token}.gif"))
            .header("cf-connecting-ip", "2001:db8::9")
            .header("x-forwarded-for", "203.0.113.7")
            .body(Body::empty())?;
        request
            .extensions_mut()
            .insert(ConnectInfo("127.0.0.1:1234".parse::<SocketAddr>()?));
        let response = router(Arc::clone(&collector)).oneshot(request).await?;
        assert_eq!(response.status(), StatusCode::OK);
        let (headers, body) =
            tokio::time::timeout(Duration::from_secs(3), receiver.recv())
                .await?
                .context("Missing private capture")?;
        assert_eq!(
            body,
            serde_json::json!({"token": token, "ip": "2001:db8::9"})
        );
        assert_eq!(
            headers
                .get("x-email-tracking-key")
                .and_then(|value| value.to_str().ok()),
            Some("capture-onlycapture-onlycapture-onlycapture-only")
        );
        assert!(!headers.contains_key("x-utils-key"));
        let permit = tokio::time::timeout(
            Duration::from_secs(3),
            collector.capacity.acquire_many(32),
        )
        .await??;
        drop(permit);
        server.abort();
        match server.await {
            Err(error) if error.is_cancelled() => Ok(()),
            result => {
                result??;
                Ok(())
            },
        }
    }
}
