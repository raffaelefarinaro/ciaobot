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
    /// Drop the Dock tile once every window is closed, leaving Ciaobot in the
    /// menu bar. Defaults to true because that was the only behaviour before
    /// this became a preference. `#[serde(default)]` on the struct means a
    /// settings file written by an older build simply gets the default.
    pub hide_dock_icon: bool,
}

impl Default for DesktopSettings {
    fn default() -> Self {
        Self {
            schema_version: 1,
            notifications_enabled: true,
            auth_bootstrapped: false,
            migration_notice_shown: false,
            hide_dock_icon: true,
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
    fn hide_dock_icon_defaults_on_for_settings_written_before_the_preference() {
        let temp = tempdir().unwrap();
        let store = SettingsStore::new(temp.path());
        // A file from a build that had no such field: serde(default) fills it,
        // so the Dock keeps behaving the way it always did.
        fs::write(
            temp.path().join("desktop-settings.json"),
            r#"{"schema_version":1,"notifications_enabled":false}"#,
        )
        .unwrap();
        let settings = store.load(None);
        assert!(settings.hide_dock_icon);
        assert!(!settings.notifications_enabled);
    }

    #[test]
    fn hide_dock_icon_round_trips_when_switched_off() {
        let temp = tempdir().unwrap();
        let store = SettingsStore::new(temp.path());
        let settings = DesktopSettings {
            hide_dock_icon: false,
            ..DesktopSettings::default()
        };
        store.save(&settings).unwrap();
        assert!(!store.load(None).hide_dock_icon);
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
