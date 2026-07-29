use crate::runtime::RuntimeConfig;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::{sync::OnceLock, time::Duration};

// One pooled client for the whole process. Building a fresh `Client` per
// request — three of them every poll — meant a new TCP handshake each time and
// no keep-alive, which is what made a tight timeout blow intermittently and the
// tray flap to "Engine: not running" while the engine was healthy.
fn http() -> &'static Client {
    static CLIENT: OnceLock<Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        Client::builder()
            .timeout(POLL_TIMEOUT)
            .pool_idle_timeout(Duration::from_secs(60))
            .build()
            .unwrap_or_else(|_| Client::new())
    })
}

// Generous relative to the ~2s poll interval: the watcher polls sequentially,
// so a slow reply only delays the next round rather than overlapping it.
const POLL_TIMEOUT: Duration = Duration::from_secs(4);

/// How many consecutive failed probes before the tray reports the engine down.
const UNREACHABLE_STRIKES: u32 = 2;

/// Debounces engine-down reporting so one slow reply cannot blank the tray.
#[derive(Debug, Default)]
pub struct ProbeTolerance {
    failures: u32,
}

impl ProbeTolerance {
    /// Returns the snapshot to publish, or `None` to keep the current one.
    ///
    /// A failed probe is only believed after `UNREACHABLE_STRIKES` in a row;
    /// until then the previous snapshot stands, so a blip no longer wipes the
    /// chat list and reset the working pulse.
    pub fn observe(&mut self, probe: Option<TraySnapshot>) -> Option<TraySnapshot> {
        match probe {
            Some(snapshot) => {
                self.failures = 0;
                Some(snapshot)
            }
            None => {
                self.failures = self.failures.saturating_add(1);
                (self.failures >= UNREACHABLE_STRIKES).then(TraySnapshot::default)
            }
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct Chat {
    pub chat_id: String,
    pub title: String,
    #[serde(default)]
    pub workspace: Option<String>,
    #[serde(default)]
    pub archived: bool,
    #[serde(default)]
    pub unread: bool,
    #[serde(default)]
    pub needs_input: bool,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq)]
pub struct StartupStatus {
    #[serde(default)]
    pub desktop_api_version: Option<u32>,
    #[serde(default)]
    pub overall_ready: bool,
    #[serde(default = "default_node_role")]
    pub node_role: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub active_peer_url: String,
    #[serde(default)]
    pub update_available: bool,
    #[serde(default, deserialize_with = "null_as_default")]
    pub latest_version: String,
}

fn default_node_role() -> String {
    "active".into()
}

// The engine sends an explicit null for these when they do not apply (a host
// has no active peer). serde(default) only covers a missing key, so null has
// to be mapped to the default separately or the whole payload fails to parse
// and the engine reads as unreachable.
fn null_as_default<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(deserializer)?.unwrap_or_default())
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq)]
pub struct TraySnapshot {
    pub reachable: bool,
    pub startup: StartupStatus,
    pub chats: Vec<Chat>,
    pub active_chat_ids: Vec<String>,
    pub attention_count: usize,
}

#[derive(Debug, Default, Deserialize)]
struct MenubarChats {
    #[serde(default)]
    chats: Vec<Chat>,
    #[serde(default)]
    attention_count: usize,
}

#[derive(Debug, Default, Deserialize)]
struct ActiveChats {
    #[serde(default)]
    active_chat_ids: Vec<String>,
}

pub async fn startup_status(runtime: &RuntimeConfig) -> Result<StartupStatus, String> {
    let url = runtime
        .server_url
        .join("api/startup-status")
        .map_err(|error| error.to_string())?;
    http()
        .get(url)
        .send()
        .await
        .map_err(|error| error.to_string())?
        .error_for_status()
        .map_err(|error| error.to_string())?
        .json()
        .await
        .map_err(|error| error.to_string())
}

async fn menubar_chats(runtime: &RuntimeConfig) -> Result<MenubarChats, String> {
    let url = runtime
        .server_url
        .join("api/menubar-chats")
        .map_err(|error| error.to_string())?;
    http()
        .get(url)
        .send()
        .await
        .map_err(|error| error.to_string())?
        .error_for_status()
        .map_err(|error| error.to_string())?
        .json::<MenubarChats>()
        .await
        .map_err(|error| error.to_string())
}

async fn active_chats(runtime: &RuntimeConfig) -> Result<Vec<String>, String> {
    let url = runtime
        .server_url
        .join("api/active-chats")
        .map_err(|error| error.to_string())?;
    http()
        .get(url)
        .send()
        .await
        .map_err(|error| error.to_string())?
        .error_for_status()
        .map_err(|error| error.to_string())?
        .json::<ActiveChats>()
        .await
        .map(|value| value.active_chat_ids)
        .map_err(|error| error.to_string())
}

/// `None` means the probe itself failed — not that the engine is down. The
/// caller decides, via [`ProbeTolerance`], when a run of failures is believable.
pub async fn tray_snapshot(runtime: &RuntimeConfig) -> Option<TraySnapshot> {
    let Ok(startup) = startup_status(runtime).await else {
        return None;
    };
    let chats = menubar_chats(runtime).await.unwrap_or_default();
    Some(TraySnapshot {
        reachable: true,
        startup,
        active_chat_ids: active_chats(runtime).await.unwrap_or_default(),
        attention_count: chats.attention_count,
        chats: chats.chats,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    // A host with no peer serialises active_peer_url as null, not as a missing
    // key, so serde(default) alone does not cover it.
    fn working_snapshot() -> TraySnapshot {
        TraySnapshot {
            reachable: true,
            active_chat_ids: vec!["chat-1".into()],
            ..TraySnapshot::default()
        }
    }

    // One slow reply used to blank the tray: "Engine: not running", no chats and
    // a reset spinner, while the engine was healthy the whole time.
    #[test]
    fn a_single_failed_probe_keeps_the_previous_snapshot() {
        let mut tolerance = ProbeTolerance::default();
        assert_eq!(
            tolerance.observe(Some(working_snapshot())),
            Some(working_snapshot())
        );
        // None => caller keeps what it already had.
        assert_eq!(tolerance.observe(None), None);
    }

    #[test]
    fn a_run_of_failures_does_report_the_engine_down() {
        let mut tolerance = ProbeTolerance::default();
        tolerance.observe(Some(working_snapshot()));
        assert_eq!(tolerance.observe(None), None);
        assert_eq!(tolerance.observe(None), Some(TraySnapshot::default()));
    }

    #[test]
    fn one_good_probe_resets_the_strike_count() {
        let mut tolerance = ProbeTolerance::default();
        assert_eq!(tolerance.observe(None), None);
        assert_eq!(
            tolerance.observe(Some(working_snapshot())),
            Some(working_snapshot())
        );
        // Back to needing a full run again, so alternating blips never flap.
        assert_eq!(tolerance.observe(None), None);
    }

    #[test]
    fn startup_status_accepts_a_null_active_peer_url() {
        let payload = r#"{
            "overall_ready": true,
            "version": "0.6.0",
            "desktop_api_version": 1,
            "node_role": "host",
            "active_peer_url": null,
            "host_url": "http://100.127.106.68:8443",
            "has_host_session": false,
            "auth_required": true
        }"#;
        let status: StartupStatus =
            serde_json::from_str(payload).expect("host payload deserialises");
        assert!(status.overall_ready);
        assert_eq!(status.node_role, "host");
        assert_eq!(status.active_peer_url, "");
        assert_eq!(status.desktop_api_version, Some(1));
    }
}
