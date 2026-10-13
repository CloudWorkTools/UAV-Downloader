#[allow(dead_code)]
#[path = "../src/client.rs"]
mod client;

use client::{DaemonClient, StartDownloadRequest};

#[tokio::test]
async fn test_client_initialization() {
    let client = DaemonClient::new("http://127.0.0.1:52341".to_string(), Some("token_123".to_string()));
    // Client initialized without panic
    assert!(format!("{:?}", client).contains("http://127.0.0.1:52341"));
}

#[test]
fn test_download_request_serialization() {
    let req = StartDownloadRequest {
        url: "https://jable.tv/videos/test/".to_string(),
        dest: Some("/downloads".to_string()),
        resolution: Some("1080".to_string()),
        max_workers: Some(4),
        enable_subtitles: Some(false),
        subtitle_mode: Some("none".to_string()),
    };

    let serialized = serde_json::to_string(&req).expect("Failed to serialize");
    assert!(serialized.contains("https://jable.tv/videos/test/"));
    assert!(serialized.contains("1080"));
}
