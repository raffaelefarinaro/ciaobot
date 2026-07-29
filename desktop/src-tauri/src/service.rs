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

pub fn resolve_ciao(path_value: Option<&str>) -> Option<PathBuf> {
    resolve_ciao_from(
        path_value,
        &[
            PathBuf::from("/opt/homebrew/bin/ciao"),
            PathBuf::from("/usr/local/bin/ciao"),
        ],
    )
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
}
