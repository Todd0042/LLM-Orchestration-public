use serde::Serialize;
use std::path::PathBuf;

#[derive(Serialize)]
pub struct NodeStatus {
    pub backend_url: String,
    pub fallback_url: String,
    pub node_token: String,
}

fn config_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("LLM_ORCH_CONFIG_DIR") {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir);
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".config").join("llm-orchestration")
}

#[tauri::command]
fn get_node_backend_url() -> NodeStatus {
    let cfg = config_dir();
    let token = std::fs::read_to_string(cfg.join("node_token"))
        .unwrap_or_default()
        .trim()
        .to_string();

    if let Ok(url) = std::env::var("LLM_ORCH_URL") {
        let trimmed = url.trim().trim_end_matches('/').to_string();
        if !trimmed.is_empty() {
            return NodeStatus {
                backend_url: trimmed,
                fallback_url: "http://127.0.0.1:8790".to_string(),
                node_token: token,
            };
        }
    }

    // If this node is configured as a Worker with a Hub URL, connect Desktop Tauri straight to the Main Hub!
    let mut primary = "http://127.0.0.1:8790".to_string();
    if let Ok(raw) = std::fs::read_to_string(cfg.join("settings.json")) {
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&raw) {
            let role = val["node"]["role"].as_str().unwrap_or("hub");
            let hub_url = val["node"]["hubUrl"].as_str().unwrap_or("").trim().trim_end_matches('/');
            if role == "worker" && !hub_url.is_empty() {
                primary = hub_url.to_string();
            }
        }
    }

    NodeStatus {
        backend_url: primary,
        fallback_url: "http://127.0.0.1:8790".to_string(),
        node_token: token,
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![get_node_backend_url])
        .run(tauri::generate_context!())
        .expect("error while running LLM-Orchestration");
}
