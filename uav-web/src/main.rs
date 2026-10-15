mod client;
mod config;
mod routes;
mod supervisor;
mod ws;

use clap::Parser;
use client::DaemonClient;
use config::AppConfig;
use routes::{create_router, AppState};
use supervisor::DaemonSupervisor;
use tracing::info;
use ws::WsHub;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,uav_web=debug".into()),
        )
        .init();

    let cfg = AppConfig::parse();
    let addr = cfg.server_addr();
    let daemon_url = cfg.daemon_base_url();

    info!("=========================================");
    info!(" 影匣 Web UI 服務啟動中");
    info!(" 網頁存取位址: http://{}", addr);
    info!(" 內部 Daemon 位址: {}", daemon_url);
    info!("=========================================");

    // Initialize Daemon Client
    let client = DaemonClient::new(daemon_url.clone(), cfg.daemon_token.clone());

    // Start Supervisor if enabled
    let supervisor = if !cfg.no_supervisor {
        let sup = DaemonSupervisor::new(
            cfg.python_path.clone(),
            cfg.daemon_host.clone(),
            cfg.daemon_port,
            cfg.daemon_token.clone(),
        );
        sup.start();
        Some(sup)
    } else {
        info!("Supervisor 已停用 (使用外部運行之 Daemon)");
        None
    };

    // Initialize WebSocket Hub
    let ws_hub = WsHub::new();
    ws_hub.clone().start_polling(client.clone());

    let state = AppState {
        client,
        ws_hub,
        static_dir: cfg.static_dir.clone(),
    };

    let app = create_router(state);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!("伺服器已成功監聽 http://{}", addr);

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal(supervisor))
        .await?;

    Ok(())
}

async fn shutdown_signal(supervisor: Option<DaemonSupervisor>) {
    let _ = tokio::signal::ctrl_c().await;
    info!("接收到關閉信號 (Ctrl+C)，正在安全退出...");
    if let Some(sup) = supervisor {
        sup.stop();
    }
}
