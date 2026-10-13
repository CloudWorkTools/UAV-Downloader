use clap::Parser;
use std::net::SocketAddr;

#[derive(Parser, Debug, Clone)]
#[command(author, version, about = "UAV Downloader Web UI & Daemon Supervisor")]
pub struct AppConfig {
    #[arg(long, default_value = "0.0.0.0", env = "UAV_HOST")]
    pub host: String,

    #[arg(short, long, default_value_t = 8080, env = "UAV_PORT")]
    pub port: u16,

    #[arg(long, default_value = "127.0.0.1", env = "UAV_DAEMON_HOST")]
    pub daemon_host: String,

    #[arg(long, default_value_t = 52341, env = "UAV_DAEMON_PORT")]
    pub daemon_port: u16,

    #[arg(long, env = "UAV_DAEMON_TOKEN")]
    pub daemon_token: Option<String>,

    #[arg(long, default_value = "python", env = "PYTHON_PATH")]
    pub python_path: String,

    #[arg(long, default_value_t = false)]
    pub no_supervisor: bool,

    #[arg(long, env = "UAV_STATIC_DIR")]
    pub static_dir: Option<String>,
}

impl AppConfig {
    pub fn server_addr(&self) -> SocketAddr {
        format!("{}:{}", self.host, self.port)
            .parse()
            .unwrap_or_else(|_| SocketAddr::from(([0, 0, 0, 0], 8080)))
    }

    pub fn daemon_base_url(&self) -> String {
        format!("http://{}:{}", self.daemon_host, self.daemon_port)
    }
}
