use crate::client::DaemonClient;
use axum::extract::ws::{Message, WebSocket};
use futures_util::StreamExt;
use serde_json::json;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::broadcast;
use tokio::time::sleep;
use tracing::debug;

pub struct WsHub {
    tx: broadcast::Sender<String>,
}

impl WsHub {
    pub fn new() -> Arc<Self> {
        let (tx, _) = broadcast::channel(100);
        Arc::new(Self { tx })
    }

    pub fn start_polling(self: Arc<Self>, client: DaemonClient) {
        tokio::spawn(async move {
            loop {
                // Poll tasks every 1 second
                match client.get_tasks().await {
                    Ok(tasks) => {
                        let msg = json!({
                            "type": "tasks",
                            "data": tasks.get("tasks").unwrap_or(&json!([]))
                        });
                        let _ = self.tx.send(msg.to_string());
                        let activity = json!({"type": "activity", "data": {
                            "activities": tasks.get("activities").unwrap_or(&json!([])),
                            "observed_at": tasks.get("observed_at")
                        }});
                        let _ = self.tx.send(activity.to_string());
                    }
                    Err(e) => {
                        debug!("Poll tasks failed (daemon reconnecting?): {}", e);
                        let msg = json!({
                            "type": "daemon_status",
                            "connected": false
                        });
                        let _ = self.tx.send(msg.to_string());
                    }
                }
                sleep(Duration::from_secs(1)).await;
            }
        });
    }

    pub async fn handle_socket(&self, mut socket: WebSocket) {
        let mut rx = self.tx.subscribe();

        loop {
            tokio::select! {
                msg = rx.recv() => {
                    match msg {
                        Ok(text) => {
                            if socket.send(Message::Text(text)).await.is_err() {
                                break;
                            }
                        }
                        Err(broadcast::error::RecvError::Lagged(_)) => continue,
                        Err(broadcast::error::RecvError::Closed) => break,
                    }
                }
                client_msg = socket.next() => {
                    match client_msg {
                        Some(Ok(Message::Close(_))) | None => break,
                        Some(Ok(Message::Ping(_))) => {
                            if socket.send(Message::Pong(vec![])).await.is_err() {
                                break;
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
    }
}
