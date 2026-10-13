use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::process::Command;
use tokio::time::sleep;
use tracing::{error, info, warn};

pub struct DaemonSupervisor {
    python_path: String,
    daemon_host: String,
    daemon_port: u16,
    daemon_token: Option<String>,
    running: Arc<AtomicBool>,
}

impl DaemonSupervisor {
    pub fn new(
        python_path: String,
        daemon_host: String,
        daemon_port: u16,
        daemon_token: Option<String>,
    ) -> Self {
        Self {
            python_path,
            daemon_host,
            daemon_port,
            daemon_token,
            running: Arc::new(AtomicBool::new(true)),
        }
    }

    pub fn start(&self) {
        let python = self.python_path.clone();
        let host = self.daemon_host.clone();
        let port = self.daemon_port;
        let token = self.daemon_token.clone();
        let running = self.running.clone();

        tokio::spawn(async move {
            info!("啟動 Python Daemon 守護進程 (Supervisor)...");
            while running.load(Ordering::SeqCst) {
                let mut cmd = Command::new(&python);
                cmd.arg("-m")
                    .arg("uav_downloader.daemon")
                    .arg("--host")
                    .arg(&host)
                    .arg("--port")
                    .arg(port.to_string());

                if let Some(t) = &token {
                    cmd.arg("--token").arg(t);
                }

                cmd.stdout(Stdio::inherit()).stderr(Stdio::inherit());

                match cmd.spawn() {
                    Ok(mut child) => {
                        info!(
                            "Python Daemon 已啟動 (PID: {:?}) 於 {}:{}",
                            child.id(),
                            host,
                            port
                        );

                        // Wait for child to exit
                        match child.wait().await {
                            Ok(status) => {
                                if !running.load(Ordering::SeqCst) {
                                    break;
                                }
                                warn!("Python Daemon 退出 (狀態: {:?})，將在 2 秒後重啟...", status);
                            }
                            Err(e) => {
                                error!("等待 Python Daemon 發生錯誤: {}", e);
                            }
                        }
                    }
                    Err(e) => {
                        error!("無法啟動 Python Daemon ({}): {}", python, e);
                        sleep(Duration::from_secs(5)).await;
                    }
                }

                if running.load(Ordering::SeqCst) {
                    sleep(Duration::from_secs(2)).await;
                }
            }
            info!("Python Daemon 守護進程已停止。");
        });
    }

    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
    }
}
