use clap::Parser;

#[allow(dead_code)]
#[path = "../src/config.rs"]
mod config;

use config::AppConfig;

#[test]
fn test_default_config() {
    let cfg = AppConfig::parse_from(["uav-web"]);
    assert_eq!(cfg.host, "0.0.0.0");
    assert_eq!(cfg.port, 8080);
    assert_eq!(cfg.daemon_host, "127.0.0.1");
    assert_eq!(cfg.daemon_port, 52341);
    assert_eq!(cfg.daemon_base_url(), "http://127.0.0.1:52341");
    assert!(!cfg.no_supervisor);
}

#[test]
fn test_custom_ports_and_host() {
    let cfg = AppConfig::parse_from([
        "uav-web",
        "--host",
        "192.168.1.100",
        "--port",
        "9090",
        "--daemon-port",
        "50000",
        "--no-supervisor",
    ]);
    assert_eq!(cfg.host, "192.168.1.100");
    assert_eq!(cfg.port, 9090);
    assert_eq!(cfg.daemon_port, 50000);
    assert_eq!(cfg.daemon_base_url(), "http://127.0.0.1:50000");
    assert!(cfg.no_supervisor);
}
