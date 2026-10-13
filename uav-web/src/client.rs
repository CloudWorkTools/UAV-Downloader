use serde::{Deserialize, Serialize};
use std::time::Duration;

#[derive(Clone, Debug)]
pub struct DaemonClient {
    base_url: String,
    auth_token: Option<String>,
    client: reqwest::Client,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct PingResponse {
    pub status: String,
    pub version: String,
    pub app: String,
    pub uptime_seconds: f64,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct StartDownloadRequest {
    pub url: String,
    pub dest: Option<String>,
    pub resolution: Option<String>,
    pub max_workers: Option<u32>,
    pub enable_subtitles: Option<bool>,
    pub subtitle_mode: Option<String>,
}

impl DaemonClient {
    pub fn new(base_url: String, auth_token: Option<String>) -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .unwrap_or_default();

        Self {
            base_url,
            auth_token,
            client,
        }
    }

    fn apply_auth(&self, req: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        if let Some(token) = &self.auth_token {
            req.header("Authorization", format!("Bearer {}", token))
        } else {
            req
        }
    }

    pub async fn ping(&self) -> Result<PingResponse, reqwest::Error> {
        let url = format!("{}/api/ping", self.base_url);
        let resp = self.apply_auth(self.client.get(&url)).send().await?;
        resp.json::<PingResponse>().await
    }

    pub async fn get_sites(&self) -> Result<serde_json::Value, reqwest::Error> {
        let url = format!("{}/api/sites", self.base_url);
        let resp = self.apply_auth(self.client.get(&url)).send().await?;
        resp.json().await
    }

    pub async fn get_categories(&self, site: &str) -> Result<serde_json::Value, reqwest::Error> {
        let url = format!("{}/api/categories?site={}", self.base_url, site);
        let resp = self.apply_auth(self.client.get(&url)).send().await?;
        resp.json().await
    }

    pub async fn get_watcher_targets(&self) -> Result<serde_json::Value, reqwest::Error> {
        let url = format!("{}/api/watcher/targets", self.base_url);
        let resp = self.apply_auth(self.client.get(&url)).send().await?;
        resp.json().await
    }

    pub async fn browse(
        &self,
        site: &str,
        url: Option<&str>,
        page: u32,
    ) -> Result<serde_json::Value, reqwest::Error> {
        let endpoint = format!("{}/api/browse", self.base_url);
        let payload = serde_json::json!({
            "site": site,
            "url": url,
            "page": page
        });
        let resp = self
            .apply_auth(self.client.post(&endpoint))
            .json(&payload)
            .send()
            .await?;
        resp.json().await
    }

    pub async fn search(
        &self,
        site: &str,
        query: &str,
        page: u32,
    ) -> Result<serde_json::Value, reqwest::Error> {
        let endpoint = format!("{}/api/search", self.base_url);
        let payload = serde_json::json!({
            "site": site,
            "query": query,
            "page": page
        });
        let resp = self
            .apply_auth(self.client.post(&endpoint))
            .json(&payload)
            .send()
            .await?;
        resp.json().await
    }

    pub async fn start_download(
        &self,
        req: StartDownloadRequest,
    ) -> Result<serde_json::Value, reqwest::Error> {
        let endpoint = format!("{}/api/download/start", self.base_url);
        let resp = self
            .apply_auth(self.client.post(&endpoint))
            .json(&req)
            .send()
            .await?;
        resp.json().await
    }

    pub async fn cancel_task(&self, task_id: &str) -> Result<serde_json::Value, reqwest::Error> {
        let endpoint = format!("{}/api/download/cancel", self.base_url);
        let payload = serde_json::json!({ "task_id": task_id });
        let resp = self
            .apply_auth(self.client.post(&endpoint))
            .json(&payload)
            .send()
            .await?;
        resp.json().await
    }

    pub async fn get_tasks(&self) -> Result<serde_json::Value, reqwest::Error> {
        let endpoint = format!("{}/api/download/tasks", self.base_url);
        let resp = self.apply_auth(self.client.get(&endpoint)).send().await?;
        resp.json().await
    }

    pub async fn get_history(
        &self,
        limit: u32,
        offset: u32,
    ) -> Result<serde_json::Value, reqwest::Error> {
        let endpoint = format!(
            "{}/api/history?limit={}&offset={}",
            self.base_url, limit, offset
        );
        let resp = self.apply_auth(self.client.get(&endpoint)).send().await?;
        resp.json().await
    }

    pub async fn check_history(
        &self,
        url: &str,
        title: &str,
    ) -> Result<serde_json::Value, reqwest::Error> {
        let endpoint = format!("{}/api/history/check", self.base_url);
        let payload = serde_json::json!({ "url": url, "title": title });
        let resp = self
            .apply_auth(self.client.post(&endpoint))
            .json(&payload)
            .send()
            .await?;
        resp.json().await
    }

    pub async fn get_settings(&self) -> Result<serde_json::Value, reqwest::Error> {
        let endpoint = format!("{}/api/settings", self.base_url);
        let resp = self.apply_auth(self.client.get(&endpoint)).send().await?;
        resp.json().await
    }

    pub async fn update_settings(
        &self,
        settings: serde_json::Value,
    ) -> Result<serde_json::Value, reqwest::Error> {
        let endpoint = format!("{}/api/settings", self.base_url);
        let resp = self
            .apply_auth(self.client.post(&endpoint))
            .json(&settings)
            .send()
            .await?;
        resp.json().await
    }
}
