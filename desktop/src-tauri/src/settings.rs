use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(default)]
pub struct DesktopSettings {
    pub schema_version: u8,
    pub notifications_enabled: bool,
    pub auth_bootstrapped: bool,
    pub migration_notice_shown: bool,
}

impl Default for DesktopSettings {
    fn default() -> Self {
        Self {
            schema_version: 1,
            notifications_enabled: true,
            auth_bootstrapped: false,
            migration_notice_shown: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct SettingsStore {
    path: PathBuf,
}

impl SettingsStore {
    pub fn new(app_data_dir: &Path) -> Self {
        Self {
            path: app_data_dir.join("desktop-settings.json"),
        }
    }

    pub fn load(&self, legacy_preferences: Option<&Path>) -> DesktopSettings {
        if let Ok(text) = fs::read_to_string(&self.path)
            && let Ok(settings) = serde_json::from_str::<DesktopSettings>(&text)
            && settings.schema_version == 1
        {
            return settings;
        }
        let mut settings = DesktopSettings::default();
        if let Some(path) = legacy_preferences
            && let Ok(text) = fs::read_to_string(path)
            && let Ok(value) = serde_json::from_str::<serde_json::Value>(&text)
            && let Some(enabled) = value.get("notifications_enabled").and_then(|v| v.as_bool())
        {
            settings.notifications_enabled = enabled;
        }
        settings
    }

    pub fn save(&self, settings: &DesktopSettings) -> io::Result<()> {
        let parent = self.path.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent)?;
        let temporary = self.path.with_extension("json.tmp");
        let bytes = serde_json::to_vec_pretty(settings)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let mut file = fs::File::create(&temporary)?;
        file.write_all(&bytes)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::rename(temporary, &self.path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn preserves_disabled_legacy_notification_preference() {
        let temp = tempdir().unwrap();
        let legacy = temp.path().join("menubar_prefs.json");
        fs::write(&legacy, r#"{"notifications_enabled":false}"#).unwrap();
        let store = SettingsStore::new(temp.path());
        assert!(!store.load(Some(&legacy)).notifications_enabled);
    }

    #[test]
    fn atomic_round_trip() {
        let temp = tempdir().unwrap();
        let store = SettingsStore::new(temp.path());
        let settings = DesktopSettings {
            notifications_enabled: false,
            ..DesktopSettings::default()
        };
        store.save(&settings).unwrap();
        assert_eq!(store.load(None), settings);
    }
}
