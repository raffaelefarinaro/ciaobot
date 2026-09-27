use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
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

// The engine transition release (#604). An existing Ciaobot.app user updates
// through the signed updater and never re-runs the one-liner, so the app itself
// has to fetch the installer and hand the engine over.

const INSTALLER_NAME: &str = "install-engine.sh";
const INSTALLER_BASE_URL: &str = "https://github.com/raffaelefarinaro/ciaobot/releases/download/";
/// The only flags the app ever passes on its own. `--migrate` is what turns a
/// re-run of the one-liner into the hand-over, and `--version` pins the run to
/// the release this app was downloaded from; `--as-host` and `--as-client URL`
/// are answers to a question only a `--migrate` run asks, and they are never
/// guessed: the installer refuses either without `--migrate`.
const MIGRATE_FLAG: &str = "--migrate";
const VERSION_FLAG: &str = "--version";
/// The program that detaches the installer, and the interpreter that runs it.
///
/// `nohup` is the detach mechanism, one of the two the design named. The app
/// exits as soon as the child is started, and `nohup` is what makes the child
/// ignore the SIGHUP that comes with a session going away; the other option,
/// `posix_spawn` with `POSIX_SPAWN_SETSID`, is not reachable from `std` without
/// a libc dependency. `sh` rather than a direct exec keeps the script's own
/// shebang out of the equation, and `/bin/sh` is the interpreter the release
/// asset is written for.
const DETACH_PROGRAM: &str = "/usr/bin/nohup";
const INSTALLER_SHELL: &str = "/bin/sh";
/// A shell script this installer cannot be: the release asset is tens of
/// kilobytes, so anything shorter is an error page that answered 200.
const MIN_INSTALLER_BYTES: usize = 1024;
const INSTALLER_FETCH_TIMEOUT: Duration = Duration::from_secs(30);

/// The pinned `install-engine.sh` URL for `version`.
///
/// Pinned to the app's own release, never `latest`: the app is about to be
/// handed over to a terminal engine, and a `latest` that moved between the
/// check and the install would run a different release than the one the user
/// was just told about.
pub fn engine_installer_url(version: &str) -> String {
    format!("{INSTALLER_BASE_URL}v{version}/{INSTALLER_NAME}")
}

/// Fetches a URL and returns its body.
///
/// Injected so [`download_engine_installer`] is testable without a network, the
/// same way [`invoke`] is testable without a launchd and an engine.
pub type HttpFn = fn(&str) -> Result<Vec<u8>, String>;

pub fn download_engine_installer(version: &str, dest_dir: &Path) -> Result<PathBuf, String> {
    download_engine_installer_from(version, dest_dir, fetch_bytes)
}

pub fn download_engine_installer_from(
    version: &str,
    dest_dir: &Path,
    http: HttpFn,
) -> Result<PathBuf, String> {
    let url = engine_installer_url(version);
    let body = http(&url)?;
    if body.len() < MIN_INSTALLER_BYTES {
        return Err(format!(
            "{url} answered with {} bytes, which is not the engine installer. \
             Check the release assets for v{version}.",
            body.len()
        ));
    }
    fs::create_dir_all(dest_dir)
        .map_err(|error| format!("could not create {}: {error}", dest_dir.display()))?;
    let path = dest_dir.join(INSTALLER_NAME);
    // 0700: the only user who can read or run the hand-over is the one the
    // migration is for.
    fs::write(&path, &body)
        .map_err(|error| format!("could not write {}: {error}", path.display()))?;
    set_owner_only_mode(&path)?;
    Ok(path)
}

#[cfg(unix)]
fn set_owner_only_mode(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .map_err(|error| format!("could not make {} executable: {error}", path.display()))
}

#[cfg(not(unix))]
fn set_owner_only_mode(_path: &Path) -> Result<(), String> {
    Ok(())
}

fn fetch_bytes(url: &str) -> Result<Vec<u8>, String> {
    // The same reqwest client the tray polls with, on the same async runtime
    // the tray watcher already blocks on. Redirects are followed, because
    // `releases/download` answers 302 to the asset's storage host, but only
    // while they stay on HTTPS: this is a script that is about to be run.
    tauri::async_runtime::block_on(async move {
        let client = reqwest::Client::builder()
            .timeout(INSTALLER_FETCH_TIMEOUT)
            .redirect(reqwest::redirect::Policy::custom(|attempt| {
                if attempt.url().scheme() == "https" {
                    attempt.follow()
                } else {
                    attempt.stop()
                }
            }))
            .build()
            .map_err(|error| format!("could not build the Ciaobot HTTP client: {error}"))?;
        let response = client
            .get(url)
            .send()
            .await
            .map_err(|error| format!("could not download {url}: {error}"))?;
        let status = response.status();
        if !status.is_success() {
            return Err(format!(
                "{url} answered {status}. The engine installer for this app version is \
                 not on the release, so the engine cannot be handed over from here."
            ));
        }
        let body = response
            .bytes()
            .await
            .map_err(|error| format!("could not read {url}: {error}"))?;
        Ok(body.to_vec())
    })
}

/// The argv the migration runs under, as the spawned process sees it.
///
/// Split out from the spawn so the shape is testable without starting a
/// process, and so [`spawn_engine_migration`] and its tests cannot disagree
/// about it: the spawner builds its `Command` from exactly this.
///
/// `version` is the running app's own version, the same value
/// [`engine_installer_url`] pins the download to. Without it the script resolves
/// `latest` for itself, and a release published between the download and the
/// run would install a different engine than the installer this app just
/// fetched. The extras follow the pin, so the script's own flags can never be
/// read as part of a version string.
pub fn migration_argv(script: &Path, version: &str, extra: &[String]) -> Vec<String> {
    let mut argv = vec![
        DETACH_PROGRAM.to_string(),
        INSTALLER_SHELL.to_string(),
        script.display().to_string(),
        MIGRATE_FLAG.to_string(),
        VERSION_FLAG.to_string(),
        version.to_string(),
    ];
    argv.extend(extra.iter().cloned());
    argv
}

/// Start the pinned installer detached, with its output appended to `log`.
///
/// The app quits immediately after this returns, so the child has to outlive it:
/// `nohup` ignores the hangup, and the child is put in its own process group so
/// nothing this app's own group receives can reach the migration.
pub fn spawn_engine_migration(
    script: &Path,
    version: &str,
    extra: &[String],
    log: &Path,
) -> Result<(), String> {
    let argv = migration_argv(script, version, extra);
    let (program, args) = argv
        .split_first()
        .ok_or_else(|| "the migration command line is empty".to_string())?;
    let log_file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log)
        .map_err(|error| format!("could not open {}: {error}", log.display()))?;
    let error_file = log_file
        .try_clone()
        .map_err(|error| format!("could not open {}: {error}", log.display()))?;
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(error_file));
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Its own group, so the installer is not in the app's: the app is about
        // to be torn down, and a signal delivered to the app's group must not
        // reach the one run taking the engine over.
        command.process_group(0);
    }
    command
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("could not start the Ciaobot engine installer: {error}"))
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

    // The download is pinned to the app's own release. A `latest` here would
    // hand a user an installer for a release they were never shown, and the app
    // is quitting before anything could report the mismatch.
    #[test]
    fn the_installer_url_is_pinned_to_the_app_version() {
        assert_eq!(
            engine_installer_url("0.18.1"),
            "https://github.com/raffaelefarinaro/ciaobot/releases/download/v0.18.1/install-engine.sh"
        );
        assert!(!engine_installer_url("0.18.1").contains("/latest/"));
    }

    fn installer_body() -> Vec<u8> {
        vec![b'#'; MIN_INSTALLER_BYTES]
    }

    fn recording_fetch() -> (HttpFn, &'static std::sync::Mutex<Vec<String>>) {
        static SEEN: std::sync::Mutex<Vec<String>> = std::sync::Mutex::new(Vec::new());
        fn fetch(url: &str) -> Result<Vec<u8>, String> {
            SEEN.lock().unwrap().push(url.to_string());
            Ok(installer_body())
        }
        (fetch as HttpFn, &SEEN)
    }

    #[test]
    fn downloads_the_pinned_installer_as_an_owner_only_script() {
        let temp = tempdir().unwrap();
        let (fetch, seen) = recording_fetch();

        let path =
            download_engine_installer_from("0.18.1", &temp.path().join("nested"), fetch).unwrap();

        assert_eq!(path, temp.path().join("nested").join("install-engine.sh"));
        assert_eq!(fs::read(&path).unwrap(), installer_body());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o700
            );
        }
        assert_eq!(
            seen.lock().unwrap().as_slice(),
            [
                "https://github.com/raffaelefarinaro/ciaobot/releases/download/v0.18.1/install-engine.sh"
            ]
        );
    }

    // A short 200 is what a proxy or an error page looks like from here, and the
    // failure it would produce later is the worst kind: a migration that runs,
    // does nothing, and reports success.
    #[test]
    fn refuses_a_response_too_short_to_be_the_installer() {
        let temp = tempdir().unwrap();
        fn short(url: &str) -> Result<Vec<u8>, String> {
            let _ = url;
            Ok(b"<html>not found</html>".to_vec())
        }

        let error = download_engine_installer_from("0.18.1", temp.path(), short as HttpFn)
            .expect_err("a 30-byte body is not the installer");

        assert!(error.contains("not the engine installer"), "{error}");
        assert!(
            !temp.path().join("install-engine.sh").exists(),
            "a refused download must not leave a script behind"
        );
    }

    #[test]
    fn a_failed_download_is_reported_with_the_url() {
        let temp = tempdir().unwrap();
        fn refused(url: &str) -> Result<Vec<u8>, String> {
            Err(format!("{url} answered 404 Not Found"))
        }

        let error =
            download_engine_installer_from("0.18.1", temp.path(), refused as HttpFn).unwrap_err();

        assert!(error.contains("404"), "{error}");
    }

    // `--migrate` is the app's own argument: it is what turns a re-run of the
    // one-liner into the hand-over, and the installer refuses an
    // `--as-host`/`--as-client` override without it.
    #[test]
    fn the_migration_runs_the_detached_shell_with_migrate() {
        let script =
            Path::new("/Users/ciao/.local/state/ciaobot/engine-migration/install-engine.sh");

        assert_eq!(
            migration_argv(script, "0.18.1", &[]),
            [
                "/usr/bin/nohup",
                "/bin/sh",
                "/Users/ciao/.local/state/ciaobot/engine-migration/install-engine.sh",
                "--migrate",
                "--version",
                "0.18.1",
            ]
        );
    }

    // Left to itself the installer resolves `latest` for the wheel and the
    // manifest it installs, so without this pin a release published between the
    // download and the run hands the user an engine nobody told them about, from
    // a script they never read. The version the installer was downloaded for is
    // the version it has to install.
    #[test]
    fn the_migration_pins_the_install_to_the_app_version() {
        let script = Path::new("/tmp/install-engine.sh");

        let argv = migration_argv(script, "0.18.1", &["--as-host".to_string()]);

        let pin = argv
            .iter()
            .position(|argument| argument == "--version")
            .expect("the hand-over installs whatever release resolves latest");
        assert_eq!(
            argv[pin + 1],
            "0.18.1",
            "the pin must carry the app version"
        );
        assert_eq!(
            argv.iter().position(|argument| argument == "--as-host"),
            Some(pin + 2),
            "the extras follow the pin, and never sit between the flag and its value"
        );
        assert_eq!(
            argv,
            [
                "/usr/bin/nohup",
                "/bin/sh",
                "/tmp/install-engine.sh",
                "--migrate",
                "--version",
                "0.18.1",
                "--as-host",
            ]
        );
    }

    #[test]
    fn the_explicit_choices_follow_migrate_as_separate_arguments() {
        let script = Path::new("/tmp/install-engine.sh");

        // One flag, no value: the installer reads it as a switch.
        assert_eq!(
            migration_argv(script, "0.18.1", &["--as-host".to_string()]),
            [
                "/usr/bin/nohup",
                "/bin/sh",
                "/tmp/install-engine.sh",
                "--migrate",
                "--version",
                "0.18.1",
                "--as-host"
            ]
        );
        // A URL as its own argument. Never joined into `--as-client=https://…`:
        // the script takes the value as the next argument and the one-liner
        // spells it the same way.
        assert_eq!(
            migration_argv(
                script,
                "0.18.1",
                &[
                    "--as-client".to_string(),
                    "https://ciao.example:8443".to_string()
                ]
            ),
            [
                "/usr/bin/nohup",
                "/bin/sh",
                "/tmp/install-engine.sh",
                "--migrate",
                "--version",
                "0.18.1",
                "--as-client",
                "https://ciao.example:8443",
            ]
        );
    }

    // The child outlives the app: the app quits as soon as it is started, so
    // what is asserted here is the two properties that decide whether it does.
    #[cfg(unix)]
    #[test]
    fn the_spawned_installer_is_detached_and_writes_to_the_log() {
        let temp = tempdir().unwrap();
        let script = temp.path().join("fake-install.sh");
        let log = temp.path().join("engine-migration.log");
        let pid_file = temp.path().join("pid");
        // A stand-in for the release asset: it ignores `--migrate`, and what it
        // does write is what the real one does - a transcript, on both streams.
        fs::write(
            &script,
            format!(
                "echo \"${{$}}\" > {pid_file}\necho migration-started\necho migration-warning >&2\n",
                pid_file = pid_file.display()
            ),
        )
        .unwrap();

        spawn_engine_migration(&script, "0.18.1", &["--as-host".to_string()], &log).unwrap();

        let transcript = wait_for_lines(&log, 2);
        assert!(
            transcript.contains("migration-started"),
            "stdout did not reach the log: {transcript}"
        );
        assert!(
            transcript.contains("migration-warning"),
            "stderr did not reach the log: {transcript}"
        );
        let installer_pid: i32 = fs::read_to_string(&pid_file)
            .expect("the installer never ran")
            .trim()
            .parse()
            .expect("the installer did not report a pid");
        let own_group = process_group(installer_pid);
        assert_ne!(
            own_group,
            current_process_group(),
            "the installer must not be in the app's process group"
        );
    }

    // Polls rather than sleeps a fixed amount: the point of the assertion is
    // that the child runs at all, not how long it takes.
    fn wait_for_lines(path: &Path, wanted: usize) -> String {
        for _ in 0..200 {
            if let Ok(text) = fs::read_to_string(path)
                && text.lines().count() >= wanted
            {
                return text;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        fs::read_to_string(path).unwrap_or_default()
    }

    #[cfg(unix)]
    fn process_group(pid: i32) -> i32 {
        let output = Command::new("/bin/ps")
            .args(["-o", "pgid=", "-p", &pid.to_string()])
            .output()
            .expect("ps");
        String::from_utf8_lossy(&output.stdout)
            .split_ascii_whitespace()
            .next()
            .and_then(|value| value.parse().ok())
            .unwrap_or(-1)
    }

    #[cfg(unix)]
    fn current_process_group() -> i32 {
        process_group(std::process::id() as i32)
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
