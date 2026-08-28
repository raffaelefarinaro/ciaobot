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

fn app_bundle_of(executable: Option<&Path>) -> Option<PathBuf> {
    executable?
        .ancestors()
        .find(|path| path.extension().and_then(|value| value.to_str()) == Some("app"))
        .map(Path::to_path_buf)
}

fn bundled_ciao_from(executable: Option<&Path>) -> Option<PathBuf> {
    let candidate = app_bundle_of(executable)?.join("Contents/Resources/ciao-runtime/bin/ciao");
    candidate.is_file().then_some(candidate)
}

/// Why [`resolve_ciao`] came up empty, as lines naming each lookup in order.
///
/// "The ciao executable was not found." alone does not separate the three ways
/// this happens, and they need different fixes: a repo-built bundle never had a
/// runtime staged into Contents/Resources (build one, or run the installed
/// app), CIAO_ENGINE_PATH points somewhere the file is not, or a dev build is
/// relying on a PATH lookup that only happens under CIAO_DEV_MODE.
fn missing_engine_detail_from(
    bundle: Option<&Path>,
    engine_path: Option<&str>,
    dev_mode: bool,
) -> String {
    let mut lines = Vec::new();
    lines.push(match bundle {
        Some(bundle) => format!(
            "• no bundled engine at {}",
            bundle
                .join("Contents/Resources/ciao-runtime/bin/ciao")
                .display()
        ),
        None => "• not running from an .app bundle, so there is no bundled engine".to_string(),
    });
    lines.push(match engine_path {
        Some(value) => format!("• CIAO_ENGINE_PATH is set to {value}, which is not a file"),
        None => "• CIAO_ENGINE_PATH is not set".to_string(),
    });
    lines.push(if dev_mode {
        "• CIAO_DEV_MODE is on, but no `ciao` is on PATH".to_string()
    } else {
        "• PATH was not searched — that fallback only runs under CIAO_DEV_MODE".to_string()
    });
    lines.join("\n")
}

/// [`missing_engine_detail_from`] against this process's environment.
pub fn missing_engine_detail() -> String {
    missing_engine_detail_from(
        app_bundle_of(env::current_exe().ok().as_deref()).as_deref(),
        env::var("CIAO_ENGINE_PATH").ok().as_deref(),
        env::var_os("CIAO_DEV_MODE").is_some(),
    )
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
        .env("CIAO_BOOTSTRAP_LAUNCHD_HANDOFF", "1")
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

/// The LaunchAgent label the installer and `ciao desktop-service` both use.
pub const SERVER_LABEL: &str = "com.ciao.server";

/// Re-register the engine's existing LaunchAgent without resolving a `ciao`
/// binary. `start_engine_if_needed` reaches this when the running bundle has
/// no bundled runtime (a repo-built `target/release/bundle` app, for one), so
/// `resolve_ciao` returns `None` — and used to give up, leaving an unloaded
/// `com.ciao.server` unloaded forever even though the plist on disk still
/// names the installed engine. launchd alone is enough to bring it back.
pub fn bootstrap_existing_service(server_plist: &Path) -> Result<(), String> {
    let uid_output = Command::new("id")
        .arg("-u")
        .output()
        .map_err(|error| format!("could not read the user id: {error}"))?;
    if !uid_output.status.success() {
        return Err("could not read the user id".to_string());
    }
    let uid = String::from_utf8_lossy(&uid_output.stdout)
        .trim()
        .to_string();
    let domain = format!("gui/{uid}");
    // Same sequence as `ciao desktop-service start`: enable, bootstrap,
    // kickstart. Bootstrap fails when the job is already registered, which is
    // not an error here — kickstart -k restarts a registered job either way,
    // so only its result decides.
    let _ = Command::new("/bin/launchctl")
        .args(["enable", &format!("{domain}/{SERVER_LABEL}")])
        .output();
    let bootstrap = Command::new("/bin/launchctl")
        .args(["bootstrap", &domain])
        .arg(server_plist)
        .output()
        .map_err(|error| format!("launchctl bootstrap failed: {error}"))?;
    let kickstart = Command::new("/bin/launchctl")
        .args(["kickstart", "-k", &format!("{domain}/{SERVER_LABEL}")])
        .output()
        .map_err(|error| format!("launchctl kickstart failed: {error}"))?;
    if !kickstart.status.success() {
        let mut detail = String::from_utf8_lossy(&kickstart.stderr)
            .trim()
            .to_string();
        if detail.is_empty() {
            detail = String::from_utf8_lossy(&bootstrap.stderr)
                .trim()
                .to_string();
        }
        if detail.is_empty() {
            detail = "launchctl reported failure".to_string();
        }
        return Err(detail);
    }
    Ok(())
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

    // The repo-built bundle: `cargo tauri build` stages no runtime, so the app
    // launches and every engine action dead-ends. Naming the path it wanted is
    // what separates that from a broken install.
    #[test]
    fn the_detail_names_the_bundle_path_it_expected() {
        let detail = missing_engine_detail_from(Some(Path::new("/tmp/Ciaobot.app")), None, false);
        assert!(
            detail.contains("/tmp/Ciaobot.app/Contents/Resources/ciao-runtime/bin/ciao"),
            "{detail}"
        );
        assert!(detail.contains("CIAO_ENGINE_PATH is not set"), "{detail}");
        assert!(detail.contains("only runs under CIAO_DEV_MODE"), "{detail}");
    }

    // A dev build run straight from target/: no bundle, and the PATH fallback
    // is the one that was supposed to work.
    #[test]
    fn the_detail_reports_a_stale_override_and_an_exhausted_path() {
        let detail = missing_engine_detail_from(None, Some("/gone/ciao"), true);
        assert!(
            detail.contains("not running from an .app bundle"),
            "{detail}"
        );
        assert!(
            detail.contains("CIAO_ENGINE_PATH is set to /gone/ciao"),
            "{detail}"
        );
        assert!(detail.contains("no `ciao` is on PATH"), "{detail}");
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
