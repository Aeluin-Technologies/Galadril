//! Worker authentication precedes body parsing and every failure hides
//! metadata.

#[path = "../src/adapters/inbound/gol/wire.rs"]
mod wire;

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::task::Poll;

use axum::body::{Body, Bytes, to_bytes};
use axum::http::{Request, StatusCode, header};
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    value: String,
}

#[tokio::test]
async fn unauthenticated_body_is_never_polled()
-> Result<(), Box<dyn std::error::Error>> {
    let polls = Arc::new(AtomicUsize::new(0));
    let observed = Arc::clone(&polls);
    let body = Body::from_stream(futures::stream::poll_fn(move |_| {
        observed.fetch_add(1, Ordering::SeqCst);
        Poll::Ready(Some(Ok::<Bytes, std::io::Error>(Bytes::from_static(
            b"secret",
        ))))
    }));
    let request = Request::builder().method("POST").body(body)?;
    let result = wire::decode::<Input>(request, 64, |_| true).await;
    assert!(result.is_err());
    assert_eq!(polls.load(Ordering::SeqCst), 0);
    Ok(())
}

#[tokio::test]
async fn missing_invalid_duplicate_or_unknown_key_never_reaches_json()
-> Result<(), Box<dyn std::error::Error>> {
    let key = "a".repeat(64);
    for values in [
        vec![],
        vec!["bad"],
        vec![key.as_str(), key.as_str()],
        vec![key.as_str()],
    ] {
        let mut request = Request::builder().method("POST");
        for value in values {
            request = request.header("x-galadril-gol-worker-key", value);
        }
        let result = wire::decode::<Input>(
            request.body(Body::from("secret malformed syntax"))?,
            64,
            |_| false,
        )
        .await;
        let Err(response) = result else {
            return Err("Invalid credentials admitted a request".into());
        };
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            response
                .headers()
                .get(header::CACHE_CONTROL)
                .and_then(|value| value.to_str().ok()),
            Some("no-store")
        );
        let body = to_bytes(response.into_body(), 1024).await?;
        assert!(!String::from_utf8_lossy(&body).contains("secret"));
    }
    Ok(())
}

#[tokio::test]
async fn admitted_json_is_bounded_and_errors_do_not_expose_names()
-> Result<(), Box<dyn std::error::Error>> {
    for body in [
        "{\"secret_field_name\":true}",
        "{",
        "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    ] {
        let request = Request::builder()
            .method("POST")
            .header("x-galadril-gol-worker-key", "a".repeat(64))
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(body))?;
        let Err(response) = wire::decode::<Input>(request, 64, |_| true).await
        else {
            return Err("Invalid JSON was accepted".into());
        };
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        assert_eq!(
            response
                .headers()
                .get(header::CACHE_CONTROL)
                .and_then(|value| value.to_str().ok()),
            Some("no-store")
        );
        let body = to_bytes(response.into_body(), 1024).await?;
        assert!(!String::from_utf8_lossy(&body).contains("secret_field_name"));
    }
    let request = Request::builder()
        .method("POST")
        .header("x-galadril-gol-worker-key", "a".repeat(64))
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from("{\"value\":\"accepted\"}"))?;
    let Ok(input) = wire::decode::<Input>(request, 64, |_| true).await else {
        return Err("Valid worker JSON was rejected".into());
    };
    assert_eq!(input.value, "accepted");
    Ok(())
}

#[tokio::test]
async fn only_private_posts_authenticate_before_their_declared_body_limit()
-> Result<(), Box<dyn std::error::Error>> {
    use axum::Router;
    use axum::middleware::{self, Next};
    use axum::response::Response;
    use axum::routing::any;

    async fn limit(request: Request<Body>, next: Next) -> Response {
        wire::enforce_content_length(request, next, 64).await
    }
    async fn handler(request: Request<Body>) -> Response {
        match wire::decode::<Input>(request, 64, |_| false).await {
            Err(response) => response,
            Ok(_) => wire::failure(StatusCode::INTERNAL_SERVER_ERROR),
        }
    }
    let router = Router::new()
        .route("/internal/gol/runtime", any(handler))
        .route("/internal/gol/effect", any(handler))
        .route("/internal/gol/runtime/extra", any(handler))
        .route("/graphql", any(handler))
        .layer(middleware::from_fn(limit));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let server =
        tokio::spawn(async move { axum::serve(listener, router).await });
    let result = async {
        let client = reqwest::Client::new();
        for (method, path, expected) in [
            ("POST", "/internal/gol/runtime", StatusCode::FORBIDDEN),
            ("POST", "/internal/gol/effect", StatusCode::FORBIDDEN),
            (
                "GET",
                "/internal/gol/runtime",
                StatusCode::PAYLOAD_TOO_LARGE,
            ),
            (
                "POST",
                "/internal/gol/runtime/extra",
                StatusCode::PAYLOAD_TOO_LARGE,
            ),
            ("POST", "/graphql", StatusCode::PAYLOAD_TOO_LARGE),
        ] {
            let response = client
                .request(method.parse()?, format!("http://{address}{path}"))
                .header(header::CONTENT_TYPE, "application/json")
                .body("x".repeat(128))
                .send()
                .await?;
            assert_eq!(response.status(), expected, "{method} {path}");
            if expected == StatusCode::FORBIDDEN {
                assert_eq!(
                    response
                        .headers()
                        .get(header::CACHE_CONTROL)
                        .and_then(|value| value.to_str().ok()),
                    Some("no-store")
                );
            }
        }
        Ok::<(), Box<dyn std::error::Error>>(())
    }
    .await;
    server.abort();
    match server.await {
        Err(error) if error.is_cancelled() => {},
        Err(error) => return Err(error.into()),
        Ok(result) => result?,
    }
    result
}
