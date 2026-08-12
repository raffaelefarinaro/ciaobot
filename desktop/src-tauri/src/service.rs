use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    env,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ServiceResult {
    pub ok: bool,
    pub action: String,
    pub message: String,
    #[serde(default)]
    pub details: Value,
}

fn resolve_ciao_from(path_value: Option<&str>, preferred: &[PathBuf]) -> Option<PathBuf> {
    for candidate in preferred {
        if candidate.is_file() {
            return Some(candidate.clone());
        }
    }
    env::split_paths(path_value.unwrap_or_default())
        .map(|directory| directory.join("ciao"))
        .find(|candidate| candidate.is_file())
}

fn bundled_ciao_from(executable: Option<&Path>) -> Option<PathBuf> {
    let app_bundle = executable?
        .ancestors()
        .find(|path| path.extension().and_then(|value| value.to_str()) == Some("app"))?;
    let candidate = app_bundle.join("Contents/Resources/ciao-runtime/bin/ciao");
    candidate.is_file().then_some(candidate)
}

pub fn resolve_ciao(path_value: Option<&str>) -> Option<PathBuf> {
    if let Some(binary) = bundled_ciao_from(env::current_exe().ok().as_deref()) {
        return Some(binary);
    }
    if let Ok(binary) = env::var("CIAO_ENGINE_PATH") {
        let candidate = PathBuf::from(binary);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    // The PATH fallback is for local development only. A packaged app must
    // always resolve its engine from Contents/Resources so another system
    // installation cannot silently become the engine for this app.
    if env::var_os("CIAO_DEV_MODE").is_some() {
        return resolve_ciao_from(path_value, &[]);
    }
    None
}

pub fn invoke(binary: &Path, action: &str, extra: &[&str]) -> Result<ServiceResult, String> {
    let mut command = Command::new(binary);
    command
        .arg("desktop-service")
        .arg(action)
        .args(extra)
        .arg("--json")
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
        .stdout(Stdio::piped());
    let output = command.output().map_err(|error| error.to_string())?;
    let result: ServiceResult = serde_json::from_slice(&output.stdout).map_err(|error| {
        let stderr = String::from_utf8_lossy(&output.stderr);
        format!("Invalid desktop-service response: {error}. {stderr}")
    })?;
    Ok(result)
}

pub fn spawn_bootstrap(binary: &Path, workspace: Option<&Path>) -> Result<(), String> {
    let mut command = Command::new(binary);
    command
        .arg("run")
        .env("CIAO_NO_BROWSER", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Some(path) = workspace {
        command.env("CIAO_WORKSPACE", path);
    }
    command
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn resolves_binary_from_path_without_a_shell() {
        let temp = tempdir().unwrap();
        let binary = temp.path().join("ciao");
        fs::write(&binary, "").unwrap();
        let path = env::join_paths([temp.path()]).unwrap();
        assert_eq!(
            resolve_ciao_from(path.to_str(), &[]).as_deref(),
            Some(binary.as_path())
        );
    }

    #[test]
    fn resolves_engine_from_the_app_bundle() {
        let temp = tempdir().unwrap();
        let executable = temp
            .path()
            .join("Ciaobot.app/Contents/MacOS/ciaobot-desktop");
        let bundled = temp
            .path()
            .join("Ciaobot.app/Contents/Resources/ciao-runtime/bin/ciao");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::create_dir_all(bundled.parent().unwrap()).unwrap();
        fs::write(&executable, "").unwrap();
        fs::write(&bundled, "").unwrap();

        assert_eq!(
            bundled_ciao_from(Some(&executable)).as_deref(),
            Some(bundled.as_path())
        );
    }
}
