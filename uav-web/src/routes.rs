use crate::client::{DaemonClient, StartDownloadRequest};
use crate::ws::WsHub;
use axum::body::{to_bytes, Body};
use axum::extract::{Query, State, WebSocketUpgrade};
use axum::http::{header, HeaderMap, HeaderValue, Method, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get, post};
use axum::{Json, Router};
use rust_embed::RustEmbed;
use serde::Deserialize;
use std::sync::Arc;

#[derive(RustEmbed)]
#[folder = "frontend/"]
pub struct Assets;

#[derive(Clone)]
pub struct AppState {
    pub client: DaemonClient,
    pub ws_hub: Arc<WsHub>,
    #[allow(dead_code)]
    pub static_dir: Option<String>,
}

#[derive(Deserialize)]
pub struct CategoriesQuery {
    pub site: Option<String>,
}

#[derive(Deserialize)]
pub struct BrowseBody {
    pub site: String,
    pub url: Option<String>,
    pub page: Option<u32>,
}

#[derive(Deserialize)]
pub struct SearchBody {
    pub site: String,
    pub query: String,
    pub page: Option<u32>,
    pub search_id: Option<String>,
    pub request_id: Option<String>,
}

#[derive(Deserialize)]
pub struct CancelBody {
    pub task_id: String,
}

#[derive(Deserialize)]
pub struct HistoryQuery {
    pub limit: Option<u32>,
    pub offset: Option<u32>,
}

#[derive(Deserialize)]
pub struct HistoryCheckBody {
    pub url: String,
    pub title: Option<String>,
}

pub fn create_router(state: AppState) -> Router {
    Router::new()
        .route("/ws", get(ws_handler))
        .route("/api/status", get(status_handler))
        .route("/api/sites", get(sites_handler))
        .route("/api/categories", get(categories_handler))
        .route("/api/watcher/targets", get(watcher_targets_handler))
        .route("/api/browse", post(browse_handler))
        .route("/api/search", post(search_handler))
        .route("/api/search/cancel", post(cancel_search_handler))
        .route("/api/download/start", post(start_download_handler))
        .route("/api/download/batch", post(download_batch_handler))
        .route("/api/download/cancel", post(cancel_download_handler))
        .route("/api/download/action", post(task_action_handler))
        .route("/api/download/tasks", get(tasks_handler))
        .route("/api/history", get(history_handler))
        .route("/api/history/check", post(history_check_handler))
        .route("/api/settings", get(get_settings_handler).post(update_settings_handler))
        .route("/api/subtitles", any(subtitles_handler))
        .route("/api/subtitles/*path", any(subtitles_handler))
        .fallback(static_handler)
        .with_state(state)
}

async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let hub = state.ws_hub.clone();
    ws.on_upgrade(move |socket| async move {
        hub.handle_socket(socket).await;
    })
}

async fn status_handler(State(state): State<AppState>) -> impl IntoResponse {
    match state.client.ping().await {
        Ok(res) => Json(serde_json::to_value(res).unwrap()).into_response(),
        Err(e) => (
            StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({ "error": format!("Daemon 連線失敗: {}", e), "connected": false })),
        )
            .into_response(),
    }
}

async fn sites_handler(State(state): State<AppState>) -> impl IntoResponse {
    match state.client.get_sites().await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn categories_handler(
    Query(q): Query<CategoriesQuery>,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let site = q.site.unwrap_or_else(|| "JableTV".to_string());
    match state.client.get_categories(&site).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn watcher_targets_handler(State(state): State<AppState>) -> impl IntoResponse {
    match state.client.get_watcher_targets().await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn browse_handler(
    State(state): State<AppState>,
    Json(body): Json<BrowseBody>,
) -> impl IntoResponse {
    let page = body.page.unwrap_or(1);
    match state.client.browse(&body.site, body.url.as_deref(), page).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn search_handler(
    State(state): State<AppState>,
    Json(body): Json<SearchBody>,
) -> impl IntoResponse {
    let page = body.page.unwrap_or(1);
    match state.client.search(&body.site, &body.query, page, body.search_id.as_deref(), body.request_id.as_deref()).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn cancel_search_handler(State(state): State<AppState>, Json(body): Json<serde_json::Value>) -> impl IntoResponse {
    match state.client.cancel_search(body).await {
        Ok((status, value)) => (StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY), Json(value)).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({"error": e.to_string()}))).into_response(),
    }
}

async fn download_batch_handler(State(state): State<AppState>, Json(body): Json<serde_json::Value>) -> impl IntoResponse {
    match state.client.download_batch(body).await {
        Ok((status, value)) => (StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY), Json(value)).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({"error": e.to_string()}))).into_response(),
    }
}

async fn start_download_handler(
    State(state): State<AppState>,
    Json(req): Json<StartDownloadRequest>,
) -> impl IntoResponse {
    match state.client.start_download(req).await {
        Ok((status, value)) => (StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY), Json(value)).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn cancel_download_handler(
    State(state): State<AppState>,
    Json(body): Json<CancelBody>,
) -> impl IntoResponse {
    match state.client.cancel_task(&body.task_id).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn tasks_handler(State(state): State<AppState>) -> impl IntoResponse {
    match state.client.get_tasks().await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn task_action_handler(State(state): State<AppState>, Json(body): Json<serde_json::Value>) -> impl IntoResponse {
    match state.client.task_action(body).await {
        Ok((status, v)) => (StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY), Json(v)).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string(), "success": false }))).into_response(),
    }
}

async fn history_handler(
    Query(q): Query<HistoryQuery>,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let limit = q.limit.unwrap_or(50);
    let offset = q.offset.unwrap_or(0);
    match state.client.get_history(limit, offset).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn history_check_handler(
    State(state): State<AppState>,
    Json(body): Json<HistoryCheckBody>,
) -> impl IntoResponse {
    let title = body.title.unwrap_or_default();
    match state.client.check_history(&body.url, &title).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn get_settings_handler(State(state): State<AppState>) -> impl IntoResponse {
    match state.client.get_settings().await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

async fn update_settings_handler(
    State(state): State<AppState>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    match state.client.update_settings(body).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(serde_json::json!({ "error": e.to_string() }))).into_response(),
    }
}

const MAX_SUBTITLE_REQUEST_BYTES: usize = 1024 * 1024;

async fn subtitles_handler(
    State(state): State<AppState>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Body,
) -> Response {
    let Some(method_allowed) = subtitle_method_allowed(uri.path(), &method) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    if !method_allowed {
        return StatusCode::METHOD_NOT_ALLOWED.into_response();
    }
    let content_type = headers.get(header::CONTENT_TYPE);
    if matches!(method, Method::POST | Method::PUT) && !is_json_content_type(content_type) {
        return StatusCode::UNSUPPORTED_MEDIA_TYPE.into_response();
    }
    let body = match to_bytes(body, MAX_SUBTITLE_REQUEST_BYTES).await {
        Ok(body) => body.to_vec(),
        Err(_) => return StatusCode::PAYLOAD_TOO_LARGE.into_response(),
    };
    let response = match state.client.subtitles_request(
        method,
        &uri.to_string(),
        content_type,
        body,
    ).await {
        Ok(response) => response,
        Err(_) => return subtitle_gateway_error(),
    };
    let status = response.status();
    let content_type = response.headers().get(reqwest::header::CONTENT_TYPE).cloned();
    let body = match response.bytes().await {
        Ok(body) => body,
        Err(_) => return subtitle_gateway_error(),
    };
    let mut result = Response::builder().status(status);
    if let Some(content_type) = content_type {
        result = result.header(header::CONTENT_TYPE, content_type);
    }
    result.body(Body::from(body)).unwrap_or_else(|_| subtitle_gateway_error())
}

fn subtitle_gateway_error() -> Response {
    (
        StatusCode::BAD_GATEWAY,
        Json(serde_json::json!({"success": false, "error": "字幕服務暫時無法連線"})),
    ).into_response()
}

fn is_json_content_type(content_type: Option<&HeaderValue>) -> bool {
    content_type
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|media_type| media_type.trim().eq_ignore_ascii_case("application/json"))
}

fn subtitle_method_allowed(path: &str, method: &Method) -> Option<bool> {
    fn is_id(value: &str) -> bool {
        !value.is_empty()
            && value.len() <= 512
            && value != "."
            && value != ".."
            && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"-_.:".contains(&byte))
    }
    fn decode_id(value: &str) -> Option<String> {
        fn hex(byte: u8) -> Option<u8> {
            match byte {
                b'0'..=b'9' => Some(byte - b'0'),
                b'a'..=b'f' => Some(byte - b'a' + 10),
                b'A'..=b'F' => Some(byte - b'A' + 10),
                _ => None,
            }
        }
        let input = value.as_bytes();
        let mut decoded = Vec::with_capacity(input.len());
        let mut index = 0;
        while index < input.len() {
            if input[index] == b'%' {
                let high = *input.get(index + 1)?;
                let low = *input.get(index + 2)?;
                decoded.push((hex(high)? << 4) | hex(low)?);
                index += 3;
            } else {
                decoded.push(input[index]);
                index += 1;
            }
        }
        let decoded = String::from_utf8(decoded).ok()?;
        is_id(&decoded).then_some(decoded)
    }
    let segments: Vec<_> = path.strip_prefix("/api/subtitles/")?.split('/').collect();
    let allowed = match segments.as_slice() {
        ["profiles"] => &[Method::GET, Method::PUT][..],
        ["profiles", "test"] => &[Method::POST][..],
        ["videos"] => &[Method::GET][..],
        ["tasks"] => &[Method::GET, Method::POST][..],
        ["tasks", task_id] if decode_id(task_id).is_some() => &[Method::GET][..],
        ["tasks", task_id, "action"] if decode_id(task_id).is_some() => &[Method::POST][..],
        ["videos", video_id, "results"] if decode_id(video_id).is_some() => &[Method::GET][..],
        ["videos", video_id, "results", result_id]
            if decode_id(video_id).is_some() && decode_id(result_id).is_some() => &[Method::GET][..],
        ["videos", video_id, "results", result_id, "content"]
            if decode_id(video_id).is_some() && decode_id(result_id).is_some() => &[Method::GET][..],
        ["videos", video_id, "results", result_id, "action"]
            if decode_id(video_id).is_some() && decode_id(result_id).is_some() => &[Method::POST][..],
        _ => return None,
    };
    Some(allowed.contains(method))
}

async fn static_handler(uri: axum::http::Uri) -> Response {
    let mut path = uri.path().trim_start_matches('/').to_string();
    if path.is_empty() {
        path = "index.html".to_string();
    }

    match Assets::get(&path) {
        Some(content) => {
            let mime = mime_guess::from_path(&path).first_or_octet_stream();
            (
                [(header::CONTENT_TYPE, HeaderValue::from_str(mime.as_ref()).unwrap())],
                content.data,
            )
                .into_response()
        }
        None => {
            // SPA fallback: return index.html for unhandled client-side routes
            if let Some(index_content) = Assets::get("index.html") {
                (
                    [(header::CONTENT_TYPE, HeaderValue::from_static("text/html; charset=utf-8"))],
                    index_content.data,
                )
                    .into_response()
            } else {
                (StatusCode::NOT_FOUND, "404 Not Found").into_response()
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::{to_bytes, Body};
    use axum::http::{Method, Request};
    use axum::routing::any;
    use std::convert::Infallible;
    use tokio::sync::{mpsc, oneshot};
    use tower::ServiceExt;

    #[derive(Debug)]
    struct SeenRequest {
        method: Method,
        uri: axum::http::Uri,
        headers: axum::http::HeaderMap,
        body: Vec<u8>,
    }

    async fn mock_daemon() -> (String, mpsc::UnboundedReceiver<SeenRequest>, oneshot::Sender<()>) {
        let (seen_tx, seen_rx) = mpsc::unbounded_channel();
        let daemon = Router::new().route(
            "/*path",
            any(move |request: Request<Body>| {
                let seen_tx = seen_tx.clone();
                async move {
                    let (parts, body) = request.into_parts();
                    let body = to_bytes(body, 1024 * 1024).await.unwrap().to_vec();
                    let status = parts.uri.query()
                        .and_then(|query| query.strip_prefix("status="))
                        .and_then(|status| status.parse::<u16>().ok())
                        .unwrap_or(409);
                    let _ = seen_tx.send(SeenRequest {
                        method: parts.method,
                        uri: parts.uri,
                        headers: parts.headers,
                        body,
                    });
                    let mut response = Response::new(Body::from(format!("daemon-{status}")));
                    *response.status_mut() = StatusCode::from_u16(status).unwrap();
                    response.headers_mut().insert(
                        header::CONTENT_TYPE,
                        HeaderValue::from_static("text/plain; charset=utf-8"),
                    );
                    Ok::<_, Infallible>(response)
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        tokio::spawn(async move {
            axum::serve(listener, daemon)
                .with_graceful_shutdown(async { let _ = shutdown_rx.await; })
                .await
                .unwrap();
        });
        (format!("http://{address}"), seen_rx, shutdown_tx)
    }

    fn app(daemon_url: String) -> Router {
        create_router(AppState {
            client: DaemonClient::new(daemon_url, Some("test-secret".into())),
            ws_hub: WsHub::new(),
            static_dir: None,
        })
    }

    #[tokio::test]
    async fn subtitle_proxy_preserves_allowed_requests_and_upstream_responses() {
        let (daemon_url, mut seen, shutdown) = mock_daemon().await;
        let requests = [
            Request::builder().method(Method::POST).uri("/api/subtitles/tasks?status=200")
                .header(header::CONTENT_TYPE, "application/json; charset=utf-8")
                .body(Body::from(r#"{"video_id":"source-v1:abc"}"#)).unwrap(),
            Request::builder().method(Method::GET).uri("/api/subtitles/tasks?status=400").body(Body::empty()).unwrap(),
            Request::builder().method(Method::GET).uri("/api/subtitles/tasks?status=404").body(Body::empty()).unwrap(),
            Request::builder().method(Method::GET).uri("/api/subtitles/tasks?status=409").body(Body::empty()).unwrap(),
            Request::builder().method(Method::GET).uri("/api/subtitles/tasks?status=413").body(Body::empty()).unwrap(),
        ];

        for request in requests {
            let response = app(daemon_url.clone()).oneshot(request).await.unwrap();
            let status = response.status();
            let content_type = response.headers().get(header::CONTENT_TYPE).unwrap().clone();
            let body = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
            let expected = format!("daemon-{}", status.as_u16());
            assert_eq!(body.as_ref(), expected.as_bytes());
            assert_eq!(content_type, "text/plain; charset=utf-8");

            let request = seen.recv().await.unwrap();
            assert_eq!(request.uri.path(), "/api/subtitles/tasks");
            assert_eq!(request.uri.query(), Some(&format!("status={}", status.as_u16())[..]));
            assert_eq!(request.headers.get(header::AUTHORIZATION).unwrap(), "Bearer test-secret");
            if status == StatusCode::OK {
                assert_eq!(request.method, Method::POST);
                assert_eq!(request.headers.get(header::CONTENT_TYPE).unwrap(), "application/json; charset=utf-8");
                assert_eq!(request.body, br#"{"video_id":"source-v1:abc"}"#);
            }
        }
        let _ = shutdown.send(());
    }

    #[tokio::test]
    async fn subtitle_proxy_rejects_unlisted_routes_methods_and_non_json_writes() {
        let (daemon_url, mut seen, shutdown) = mock_daemon().await;
        let app = app(daemon_url);
        for (method, uri, content_type) in [
            (Method::GET, "/api/subtitles/unlisted", None),
            (Method::DELETE, "/api/subtitles/tasks", None),
            (Method::POST, "/api/subtitles/tasks", Some("text/plain")),
        ] {
            let mut request = Request::builder().method(method).uri(uri);
            if let Some(value) = content_type {
                request = request.header(header::CONTENT_TYPE, value);
            }
            let response = app.clone().oneshot(request.body(Body::from("x")).unwrap()).await.unwrap();
            assert!(matches!(response.status(), StatusCode::NOT_FOUND | StatusCode::METHOD_NOT_ALLOWED | StatusCode::UNSUPPORTED_MEDIA_TYPE));
        }
        let oversized = vec![b'x'; MAX_SUBTITLE_REQUEST_BYTES + 1];
        let response = app.clone().oneshot(
            Request::builder().method(Method::POST).uri("/api/subtitles/tasks")
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(oversized)).unwrap(),
        ).await.unwrap();
        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
        assert!(seen.try_recv().is_err(), "rejected requests must not reach the daemon");
        let _ = shutdown.send(());
    }

    #[tokio::test]
    async fn subtitle_proxy_allows_only_the_documented_endpoint_shapes() {
        let (daemon_url, mut seen, shutdown) = mock_daemon().await;
        let app = app(daemon_url);
        let allowed = [
            (Method::GET, "/api/subtitles/profiles"),
            (Method::PUT, "/api/subtitles/profiles"),
            (Method::POST, "/api/subtitles/profiles/test"),
            (Method::GET, "/api/subtitles/videos"),
            (Method::GET, "/api/subtitles/tasks"),
            (Method::POST, "/api/subtitles/tasks"),
            (Method::GET, "/api/subtitles/tasks/subtitle_0123456789abcdef"),
            (Method::POST, "/api/subtitles/tasks/subtitle_0123456789abcdef/action"),
            (Method::GET, "/api/subtitles/videos/source-v1:abc/results"),
            (Method::GET, "/api/subtitles/videos/source-v1:abc/results/result-1"),
            (Method::GET, "/api/subtitles/videos/source-v1:abc/results/result-1/content"),
            (Method::POST, "/api/subtitles/videos/source-v1:abc/results/result-1/action"),
        ];
        for (method, uri) in allowed {
            let mut request = Request::builder().method(method.clone()).uri(uri);
            if matches!(method, Method::POST | Method::PUT) {
                request = request.header(header::CONTENT_TYPE, "application/json");
            }
            let response = app.clone().oneshot(request.body(Body::from("{}")).unwrap()).await.unwrap();
            assert_eq!(response.status(), StatusCode::CONFLICT, "{method} {uri}");
            let forwarded = seen.recv().await.unwrap();
            assert_eq!(forwarded.method, method);
            assert_eq!(forwarded.uri.path(), uri);
        }
        let _ = shutdown.send(());
    }

    #[tokio::test]
    async fn subtitle_proxy_decodes_encoded_ids_for_validation_without_widening_routes() {
        let (daemon_url, mut seen, shutdown) = mock_daemon().await;
        let app = app(daemon_url);
        let encoded = "/api/subtitles/videos/source-v1%3Aabc/results/result%3A1";
        let response = app.clone().oneshot(Request::get(encoded).body(Body::empty()).unwrap()).await.unwrap();
        assert_eq!(response.status(), StatusCode::CONFLICT);
        let forwarded = seen.recv().await.unwrap();
        assert_eq!(forwarded.uri.path(), encoded);

        for path in [
            "/api/subtitles/videos/source-v1%2Fabc/results/result-1",
            "/api/subtitles/videos/%2e%2e/results/result-1",
            "/api/subtitles/videos/source-v1%3Aabc/results/%2e%2e",
        ] {
            let response = app.clone().oneshot(Request::get(path).body(Body::empty()).unwrap()).await.unwrap();
            assert_eq!(response.status(), StatusCode::NOT_FOUND, "{path}");
        }
        assert!(seen.try_recv().is_err(), "encoded slash/traversal IDs must not reach daemon");
        let _ = shutdown.send(());
    }

    #[tokio::test]
    async fn unknown_client_route_keeps_spa_fallback() {
        let response = app("http://127.0.0.1:1".into())
            .oneshot(Request::get("/client/navigation").body(Body::empty()).unwrap())
            .await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert!(response.headers()[header::CONTENT_TYPE].to_str().unwrap().starts_with("text/html"));
    }
}
