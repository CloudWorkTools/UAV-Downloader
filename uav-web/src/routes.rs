use crate::client::{DaemonClient, StartDownloadRequest};
use crate::ws::WsHub;
use axum::extract::{Query, State, WebSocketUpgrade};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
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
        .route("/api/download/cancel", post(cancel_download_handler))
        .route("/api/download/action", post(task_action_handler))
        .route("/api/download/tasks", get(tasks_handler))
        .route("/api/history", get(history_handler))
        .route("/api/history/check", post(history_check_handler))
        .route("/api/settings", get(get_settings_handler).post(update_settings_handler))
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

async fn start_download_handler(
    State(state): State<AppState>,
    Json(req): Json<StartDownloadRequest>,
) -> impl IntoResponse {
    match state.client.start_download(req).await {
        Ok(v) => Json(v).into_response(),
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
