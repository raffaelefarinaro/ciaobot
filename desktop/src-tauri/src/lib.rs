mod capture;
mod native_notifications;
mod notification_log;
mod permissions;
mod runtime;
mod service;
mod settings;
mod tray;

use crate::{
    capture::TraySnapshot,
    native_notifications::{NativeNotification, NavigationIntent, install_action_listener},
    notification_log::NotificationLogTail,
    runtime::RuntimeConfig,
    settings::{DesktopSettings, SettingsStore},
};
use std::{
    env,
    net::{Ipv4Addr, SocketAddr, TcpStream},
    process::{Command, Stdio},
    sync::{Arc, Mutex, RwLock},
    thread,
    time::Duration,
};
use tauri::{
    AppHandle, DragDropEvent, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent, image::Image,
    tray::TrayIconBuilder, webview::NewWindowResponse,
};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as AutostartExt};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

struct DesktopModel {
    runtime: RwLock<RuntimeConfig>,
    settings: Mutex<DesktopSettings>,
    store: SettingsStore,
    pending_navigation: Mutex<Option<NavigationIntent>>,
    tray_snapshot: RwLock<TraySnapshot>,
    main_url: Arc<RwLock<url::Url>>,
    // Rows for chats that are working, retained so the animation thread can
    // swap their pulsing-dot icon without rebuilding the whole menu.
    working_items: Mutex<Vec<tray::WorkingRow>>,
}

// Only hand schemes to /usr/bin/open that a link in a chat can legitimately
// use. Anything else (file, javascript, custom app schemes) would let page
// content ask the system to open arbitrary local targets.
fn is_external_link(url: &url::Url) -> bool {
    matches!(url.scheme(), "http" | "https" | "mailto")
}

fn same_origin(left: &url::Url, right: &url::Url) -> bool {
    left.scheme() == right.scheme()
        && left.host_str() == right.host_str()
        && left.port_or_known_default() == right.port_or_known_default()
}

fn is_trusted_main_navigation(url: &url::Url, server_url: &url::Url) -> bool {
    if matches!(url.scheme(), "tauri" | "asset")
        || url.host_str().is_some_and(|host| host == "tauri.localhost")
    {
        return true;
    }
    if same_origin(url, server_url) {
        return true;
    }
    cfg!(debug_assertions)
        && url.host_str().is_some_and(|host| host == "localhost")
        && url.port_or_known_default() == Some(1420)
}

fn browser_event_script(name: &str, detail: &serde_json::Value) -> String {
    let name = serde_json::to_string(name).unwrap_or_else(|_| "\"ciao:native-drop-error\"".into());
    let detail = serde_json::to_string(detail).unwrap_or_else(|_| "{}".into());
    format!("window.dispatchEvent(new CustomEvent({name}, {{ detail: {detail} }}));")
}

// Chat image extensions, mirroring `_ALLOWED_IMAGE_EXTENSIONS` in
// ciao/web/project_chats.py. Only these get staged: the server copies their
// bytes into media_root, so a staged copy is safe to delete afterwards, while a
// non-image drop is handed to the agent as a path it has to keep reading.
const DROP_IMAGE_EXTENSIONS: [&str; 5] = ["png", "jpg", "jpeg", "gif", "webp"];

// Bounds how much a single dropped file pulls into memory. A screenshot is
// never close; a promise-backed drag of something else can be.
const DROP_STAGING_MAX_BYTES: u64 = 64 * 1024 * 1024;

// `SF_DATALESS`, from `sys/stat.h`. macOS sets it on a cloud file whose bytes
// live only in the provider: `stat` reports the real size, `st_blocks` is 0, and
// a read has to materialise the file first.
const SF_DATALESS: u32 = 0x4000_0000;

/// True for `st_flags` marking a file the File Provider has not materialised.
///
/// Split out from the `stat` call so the bit test is unit-testable: a dataless
/// file cannot be created in a temp dir.
fn flags_are_dataless(flags: u32) -> bool {
    flags & SF_DATALESS != 0
}

#[cfg(target_os = "macos")]
fn is_dataless(metadata: &std::fs::Metadata) -> bool {
    use std::os::macos::fs::MetadataExt;
    flags_are_dataless(metadata.st_flags())
}

#[cfg(not(target_os = "macos"))]
fn is_dataless(_metadata: &std::fs::Metadata) -> bool {
    false
}

/// True for a drop the Python server will not be able to read itself.
///
/// Two cases, both of which left the drop on the floor as a raw errno:
///
/// - Dragging straight from the macOS screenshot thumbnail hands over a path in
///   `.../TemporaryItems/NSIRD_screencaptureui_*/`. macOS grants that read to
///   the app which received the drop, this process, and denies it to every other
///   one, so the server's `read_bytes()` failed with `EPERM` (issue #238).
/// - Dragging a file out of iCloud Drive (or any other File Provider) hands over
///   a path whose bytes are not on disk. Materialising it is refused for a
///   process that may not talk to the provider, which the server is, so its
///   `read_bytes()` failed with `EDEADLK` — "Resource deadlock avoided".
///
/// This app *is* allowed both reads, so staging a copy here is what makes the
/// drop work at all. Gated rather than applied to every file so an ordinary
/// local drag stays zero-copy, because staging a dataless file means waiting
/// for iCloud to hand the bytes over. The NSIRD screenshot dir is a permissions
/// quirk that only ever holds images — staging a non-image from it would break
/// the agent's continuing read access — while a dataless File Provider file can
/// be anything, so it is staged regardless of type. The caller keeps the wait
/// off the window event thread and the size cap (`DROP_STAGING_MAX_BYTES`)
/// applies to every staged copy.
fn needs_drop_staging(path: &std::path::Path) -> bool {
    let is_image = path
        .extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| {
            DROP_IMAGE_EXTENSIONS.contains(&extension.to_ascii_lowercase().as_str())
        });
    let is_promise_backed = path.components().any(|component| {
        component
            .as_os_str()
            .to_str()
            .is_some_and(|name| name.starts_with("NSIRD_"))
    });
    should_stage(
        is_image,
        is_promise_backed,
        std::fs::metadata(path).is_ok_and(|metadata| metadata.is_file() && is_dataless(&metadata)),
    )
}

/// The staging decision, split out so the dataless branch is unit-testable: a
/// dataless file cannot be created in a temp dir (see `flags_are_dataless`).
fn should_stage(is_image: bool, is_promise_backed: bool, is_dataless: bool) -> bool {
    if is_promise_backed {
        return is_image;
    }
    is_dataless
}

/// Copy one dropped file into this grant's staging dir, returning the new path.
///
/// Reads and writes the bytes rather than calling `std::fs::copy`, which on
/// macOS uses `fcopyfile` and carries the source's extended attributes across:
/// the copy has to be an ordinary file any process can open. Returns None on
/// failure so the caller keeps the original path and the server reports one
/// per-file error instead of the whole drop failing. Each file gets its own
/// numbered subdirectory, so two dropped files sharing a name do not collide.
fn stage_dropped_file(
    staging_dir: &std::path::Path,
    index: usize,
    path: &std::path::Path,
) -> Option<std::path::PathBuf> {
    if std::fs::metadata(path).ok()?.len() > DROP_STAGING_MAX_BYTES {
        return None;
    }
    let name = path.file_name()?;
    let target_dir = staging_dir.join(index.to_string());
    std::fs::create_dir_all(&target_dir).ok()?;
    let target = target_dir.join(name);
    let data = std::fs::read(path).ok()?;
    std::fs::write(&target, &data).ok()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o600)).ok()?;
    }
    Some(target)
}

fn create_desktop_drop_grant(
    runtime_root: &std::path::Path,
    paths: &[std::path::PathBuf],
) -> Result<(String, Vec<String>), String> {
    let grant_id = uuid::Uuid::new_v4().to_string();
    let grant_dir = runtime_root.join("desktop-drop-grants");
    std::fs::create_dir_all(&grant_dir).map_err(|error| error.to_string())?;
    if let Ok(entries) = std::fs::read_dir(&grant_dir) {
        for entry in entries.flatten() {
            let is_stale = entry
                .metadata()
                .and_then(|metadata| metadata.modified())
                .ok()
                .and_then(|modified| modified.elapsed().ok())
                .is_some_and(|age| age > Duration::from_secs(10 * 60));
            if is_stale {
                // `staged/` is a directory, so sweep both shapes. The server
                // deletes a staged copy as soon as it holds the bytes; this is
                // the backstop for a grant that was never consumed.
                let stale_path = entry.path();
                if stale_path.is_dir() {
                    let _ = std::fs::remove_dir_all(&stale_path);
                } else {
                    let _ = std::fs::remove_file(&stale_path);
                }
            }
        }
    }
    let staging_dir = grant_dir.join("staged").join(&grant_id);
    let paths = paths
        .iter()
        .enumerate()
        .map(|(index, path)| {
            if needs_drop_staging(path)
                && let Some(staged) = stage_dropped_file(&staging_dir, index, path)
            {
                return staged.to_string_lossy().into_owned();
            }
            path.to_string_lossy().into_owned()
        })
        .collect::<Vec<_>>();
    let grant_path = grant_dir.join(format!("{grant_id}.json"));
    let temp_path = grant_dir.join(format!(".{grant_id}.tmp"));
    let created_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_secs();
    let payload = serde_json::json!({
        "created_at": created_at,
        "paths": paths,
    });
    std::fs::write(
        &temp_path,
        serde_json::to_vec(&payload).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&temp_path, std::fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
    }
    std::fs::rename(&temp_path, &grant_path).map_err(|error| error.to_string())?;
    Ok((grant_id, paths))
}

fn notification_permission_state() -> String {
    mac_usernotifications::blocking::get_notification_settings()
        .map(|settings| format!("{:?}", settings.authorization_status).to_lowercase())
        .unwrap_or_else(|_| "unavailable".into())
}

fn maybe_request_notification_permission(app: &AppHandle, enabled: bool) {
    if !enabled || !notification_permission_state().contains("notdetermined") {
        return;
    }
    app.dialog()
        .message(
            "Ciaobot uses notifications for completed work, permission requests, questions, and workspace connection problems.",
        )
        .title("Allow Ciaobot notifications?")
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Continue".into(),
            "Not Now".into(),
        ))
        .show(|confirmed| {
            if confirmed {
                thread::spawn(|| {
                    let _ = mac_usernotifications::blocking::request_auth();
                });
            }
        });
}

fn maybe_show_browser_pwa_notice(app: &AppHandle, migration: &service::ServiceResult) {
    let paths = migration
        .details
        .get("browser_pwa_paths")
        .and_then(serde_json::Value::as_array)
        .map_or(0, Vec::len);
    if paths == 0 {
        return;
    }
    let model = app.state::<DesktopModel>();
    let should_show = model
        .settings
        .lock()
        .ok()
        .is_some_and(|settings| !settings.migration_notice_shown);
    if !should_show {
        return;
    }
    if let Ok(mut settings) = model.settings.lock() {
        settings.migration_notice_shown = true;
        let _ = model.store.save(&settings);
    }
    app.dialog()
        .message(
            "A browser-installed Ciaobot app was found. Ciaobot.app is now the native desktop app; you may remove the browser copy when convenient.",
        )
        .title("Ciaobot desktop migration")
        .show(|_| {});
}

// `update-engine` reports ok=false when the upgrade was a no-op so the PWA's
// update banner does not clear early. For the combined update that is "nothing
// to do", not a failure, so read the structured flag rather than the message.
fn engine_already_current(result: &service::ServiceResult) -> bool {
    result
        .details
        .get("already_current")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false)
}

fn requires_confirmation(result: &service::ServiceResult) -> bool {
    result
        .details
        .get("requires_confirmation")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false)
}

fn engine_launch_action(result: &service::ServiceResult) -> &'static str {
    if result
        .details
        .get("loaded")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false)
    {
        "restart"
    } else {
        "start"
    }
}

// Restart the separate Python LaunchAgent after the app bundle has been
// replaced. The app updater only restarts this Tauri process; without this
// kickstart the server keeps executing the old bundle from the same plist path.
// A fresh bootstrap install has no server plist yet, so there is nothing to
// restart until onboarding finishes setup.
fn restart_engine_after_app_update(app: &AppHandle) -> Result<(), String> {
    let server_plist = app
        .state::<DesktopModel>()
        .runtime
        .read()
        .map_err(|_| "Could not read the current Ciaobot runtime.".to_string())?
        .server_plist
        .clone();
    if !server_plist.is_file() {
        return Ok(());
    }
    let binary = service::resolve_ciao(env::var("PATH").ok().as_deref())
        .ok_or_else(|| "The bundled Ciaobot engine was not found.".to_string())?;
    let status = service::invoke(&binary, "status", &[])?;
    let action = engine_launch_action(&status);
    let result = service::invoke(
        &binary,
        action,
        if action == "restart" {
            &["--force"][..]
        } else {
            &[][..]
        },
    )?;
    if result.ok {
        Ok(())
    } else {
        Err(result.message)
    }
}

// The download owns the 60..=88 band of the update window's progress bar.
// `downloaded` is the running total of bytes received, not the last chunk.
// Without a Content-Length there is nothing to divide by, so the bar parks
// mid-band rather than pretending to advance.
fn download_percent(downloaded: u64, total: Option<u64>) -> u8 {
    match total {
        None => 70,
        Some(0) => 72,
        Some(total) => 60 + (downloaded.saturating_mul(28) / total).min(28) as u8,
    }
}

// Second half of the unified update. The dedicated update window stays visible
// while the engine and app halves move together, then the process restarts.
// Only restart when something actually changed — otherwise "Update…" would
// bounce a healthy app for nothing.
async fn install_app_update(app: AppHandle, engine_updated: bool) -> Result<(), String> {
    emit_update_progress(
        &app,
        54,
        "checking the latest Ciaobot release",
        vec![
            "[ciao] local engine checked ....................... ok".into(),
            "[ciao] checking the latest signed app release ...... in progress".into(),
        ],
        "running",
    );
    let updater = app.updater().map_err(|error| error.to_string())?;
    let update = updater.check().await.map_err(|error| error.to_string())?;
    match update {
        Some(update) => {
            let version = update.version.clone();
            emit_update_progress(
                &app,
                60,
                &format!("downloading Ciaobot {version}"),
                vec![
                    "[ciao] local engine checked ....................... ok".into(),
                    format!("[ciao] downloading signed Ciaobot {version} ........ in progress"),
                ],
                "running",
            );
            let download_app = app.clone();
            let download_version = version.clone();
            // `on_chunk` reports the size of *this* chunk, not the running
            // total, so feeding it straight into the percentage pinned the bar
            // at whatever the first chunk happened to be (~60%) for the whole
            // download. Keep the total here.
            let mut downloaded: u64 = 0;
            update
                .download_and_install(
                    move |chunk, total| {
                        downloaded = downloaded.saturating_add(chunk as u64);
                        let percent = download_percent(downloaded, total);
                        emit_update_progress(
                            &download_app,
                            percent,
                            &format!("downloading Ciaobot {download_version}"),
                            vec![
                                "[ciao] local engine checked ....................... ok".into(),
                                format!(
                                    "[ciao] downloading signed Ciaobot {download_version} ........ {percent}%"
                                ),
                            ],
                            "running",
                        );
                    },
                    {
                        let install_app = app.clone();
                        let install_version = version.clone();
                        move || {
                            emit_update_progress(
                                &install_app,
                                90,
                                "installing the signed app bundle",
                                vec![
                                    "[ciao] local engine checked ....................... ok".into(),
                                    format!("[ciao] downloaded signed Ciaobot {install_version} ........ ok"),
                                    "[ciao] installing the signed app bundle ........... in progress".into(),
                                ],
                                "running",
                            );
                        }
                    },
                )
                .await
                .map_err(|error| format!("Update {version} could not be installed: {error}"))?;
            // The bundle has already been swapped on disk at this point, so the
            // running process is the *old* app. Propagating a restart failure
            // out of here skipped `app.restart()` and left exactly that: a new
            // bundle on disk, an old app in memory, and a half-restarted
            // engine — the desktop-service version mismatch this whole path
            // exists to avoid. Surface the engine failure, then restart anyway;
            // the fresh process runs the engine start-up path again.
            if let Err(error) = restart_engine_after_app_update(&app) {
                tray_log(
                    &app,
                    &format!("engine restart after app update FAILED: {error}"),
                );
                // Blocking: the `?` this replaced at least produced an error
                // dialog, and `app.restart()` below would outrun a
                // fire-and-forget one. Safe here - this is a spawned async
                // task, not the main thread.
                show_error_blocking(
                    &app,
                    "Engine restart failed",
                    format!(
                        "The app was updated, but the Ciaobot engine did not \
                         restart:\n\n{error}\n\nCiaobot will restart now. If \
                         the engine stays down, use Restart engine in the menu \
                         bar."
                    ),
                );
            }
            emit_update_progress(
                &app,
                98,
                "restarting Ciaobot with the latest version",
                vec![
                    "[ciao] local engine checked ....................... ok".into(),
                    format!("[ciao] downloaded signed Ciaobot {version} ........ ok"),
                    "[ciao] installed app bundle ........................ ok".into(),
                    "[ciao] restarting Ciaobot .......................... in progress".into(),
                ],
                "running",
            );
            app.restart()
        }
        // The engine moved but the app did not: restart so both halves come
        // back on the version the engine is now running.
        None if engine_updated => {
            emit_update_progress(
                &app,
                98,
                "restarting Ciaobot with the updated engine",
                vec![
                    "[ciao] local engine updated ....................... ok".into(),
                    "[ciao] no newer app bundle was needed ................ ok".into(),
                    "[ciao] restarting Ciaobot .......................... in progress".into(),
                ],
                "running",
            );
            app.restart()
        }
        None => Ok(()),
    }
}

// One update for both halves: they ship from the same tag, so updating one
// without the other is what produces an opaque desktop-service mismatch.
fn run_full_update(app: AppHandle, force: bool) {
    thread::spawn(move || {
        show_update_screen(
            &app,
            4,
            "checking active work before updating",
            vec![
                "[ciao] opening the update flow ..................... ok".into(),
                "[ciao] checking active chats ........................ in progress".into(),
            ],
        );
        let Some(binary) = service::resolve_ciao(env::var("PATH").ok().as_deref()) else {
            hide_update_window(&app);
            show_window(&app, "main");
            show_error(
                &app,
                "Ciaobot engine unavailable",
                "The ciao executable was not found.",
            );
            return;
        };
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.hide();
        }
        emit_update_progress(
            &app,
            12,
            "updating the local engine",
            vec![
                "[ciao] opening the update flow ..................... ok".into(),
                "[ciao] active chats checked ........................ ok".into(),
                "[ciao] updating the local engine ................... in progress".into(),
            ],
            "running",
        );
        let extra = if force { &["--force"][..] } else { &[][..] };
        let engine_updated = match service::invoke(&binary, "update-engine", extra) {
            Ok(result) if result.ok => {
                emit_update_progress(
                    &app,
                    48,
                    "local engine updated; checking the app release",
                    vec![
                        "[ciao] active chats checked ........................ ok".into(),
                        "[ciao] local engine updated ....................... ok".into(),
                        "[ciao] checking the signed app release ............ in progress".into(),
                    ],
                    "running",
                );
                true
            }
            Ok(result) if engine_already_current(&result) => {
                emit_update_progress(
                    &app,
                    48,
                    "local engine is current; checking the app release",
                    vec![
                        "[ciao] active chats checked ........................ ok".into(),
                        "[ciao] local engine is already current ............ ok".into(),
                        "[ciao] checking the signed app release ............ in progress".into(),
                    ],
                    "running",
                );
                false
            }
            Ok(result) if !force && requires_confirmation(&result) => {
                hide_update_window(&app);
                show_window(&app, "main");
                prompt_forced_update(app.clone(), &result);
                return;
            }
            Ok(result) => {
                hide_update_window(&app);
                show_window(&app, "main");
                show_error(&app, "Could not update Ciaobot", result.message);
                return;
            }
            Err(error) => {
                hide_update_window(&app);
                show_window(&app, "main");
                show_error(&app, "Could not update Ciaobot", error);
                return;
            }
        };
        let updater_app = app.clone();
        tauri::async_runtime::spawn(async move {
            match install_app_update(updater_app.clone(), engine_updated).await {
                // Nothing was installed and there is no restart coming, so put
                // back the window the update hid on the way in.
                Ok(()) => {
                    hide_update_window(&updater_app);
                    show_window(&updater_app, "main");
                    show_info(
                        &updater_app,
                        "Ciaobot is up to date",
                        "The engine and the app are both on the latest available version.",
                    );
                }
                Err(error) => {
                    emit_update_progress(
                        &updater_app,
                        60,
                        "update failed",
                        vec![format!(
                            "[ciao] update failed ............................. {error}"
                        )],
                        "error",
                    );
                    hide_update_window(&updater_app);
                    show_window(&updater_app, "main");
                    show_error(&updater_app, "Could not update Ciaobot", error);
                }
            }
        });
    });
}

fn prompt_forced_update(app: AppHandle, result: &service::ServiceResult) {
    let count = result
        .details
        .get("active_chat_ids")
        .and_then(serde_json::Value::as_array)
        .map_or(0, Vec::len);
    let detail = if count == 0 {
        "Active work will be interrupted.".to_string()
    } else {
        format!("{count} chat(s) are working. Updating will interrupt them.")
    };
    app.dialog()
        .message(detail)
        .title("Update anyway?")
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Update".into(),
            "Cancel".into(),
        ))
        .show(move |confirmed| {
            if confirmed {
                run_full_update(app, true);
            }
        });
}

// Ciaobot lives in the menu bar, so the Dock tile is only meaningful while a
// window is on screen. Accessory hides the tile (and the app menu) without
// touching the tray; Regular brings it back so the window can be activated and
// reached from the Dock and app switcher like any normal app.
fn set_dock_visible(app: &AppHandle, visible: bool) {
    let policy = if visible {
        tauri::ActivationPolicy::Regular
    } else {
        tauri::ActivationPolicy::Accessory
    };
    let _ = app.set_activation_policy(policy);
}

// Whether the Dock tile should disappear along with the last window. Reading the
// setting through a poisoned-lock-tolerant helper keeps the window-close path
// from panicking on a lock another thread died holding; defaulting to true there
// preserves the behaviour this preference replaced.
fn hide_dock_icon_enabled(app: &AppHandle) -> bool {
    app.state::<DesktopModel>()
        .settings
        .lock()
        .map(|settings| settings.hide_dock_icon)
        .unwrap_or(true)
}

fn hide_dock_unless_pinned(app: &AppHandle) {
    if hide_dock_icon_enabled(app) {
        set_dock_visible(app, false);
    }
}

fn show_window(app: &AppHandle, label: &str) {
    if let Some(window) = app.get_webview_window(label) {
        // Switch to Regular *before* showing: an Accessory app cannot become
        // the active app, so the window would appear without keyboard focus.
        set_dock_visible(app, true);
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn hide_update_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("update") {
        let _ = window.hide();
    }
}

fn emit_update_progress(
    app: &AppHandle,
    percent: u8,
    message: &str,
    lines: Vec<String>,
    status: &str,
) {
    let detail = serde_json::json!({
        "percent": percent,
        "message": message,
        "lines": lines,
        "status": status,
    });
    if let Some(window) = app.get_webview_window("update") {
        let _ = window.eval(browser_event_script("ciao:update-progress", &detail));
    }
}

fn show_update_screen(app: &AppHandle, percent: u8, message: &str, lines: Vec<String>) {
    show_window(app, "update");
    emit_update_progress(app, percent, message, lines, "running");
}

fn url_with_segments(runtime: &RuntimeConfig, segments: &[&str]) -> Result<url::Url, String> {
    let mut url = runtime.server_url.clone();
    {
        let mut path = url
            .path_segments_mut()
            .map_err(|_| "The configured server URL cannot be used for navigation.".to_string())?;
        path.clear();
        path.extend(segments.iter().copied());
    }
    Ok(url)
}

async fn try_apply_pending_navigation(app: AppHandle) {
    let intent = app
        .state::<DesktopModel>()
        .pending_navigation
        .lock()
        .ok()
        .and_then(|pending| pending.clone());
    let Some(intent) = intent else {
        return;
    };
    let runtime = match app.state::<DesktopModel>().runtime.read() {
        Ok(runtime) => runtime.clone(),
        Err(_) => return,
    };
    let Some(main) = app.get_webview_window("main") else {
        return;
    };
    let applied = match &intent {
        NavigationIntent::Chat(chat_id) => {
            let is_login = app
                .state::<DesktopModel>()
                .main_url
                .read()
                .is_ok_and(|url| url.path().starts_with("/login"));
            if is_login {
                false
            } else {
                let endpoint = match url_with_segments(&runtime, &["api", "open-chat", chat_id]) {
                    Ok(endpoint) => endpoint,
                    Err(_) => return,
                };
                // A chat deep-link only counts as applied once a live PWA client
                // confirms it received the `open_chat` event. On a cold start the
                // /ws/events socket has no subscriber yet, so `delivered` is false
                // and navigating the just-created webview to /chat/<id> is racy. If
                // we cleared the intent here the first notification click would only
                // open the app. Leave the intent pending instead so the runtime
                // watcher retries it every few seconds until the PWA is up.
                match reqwest::Client::new()
                    .get(endpoint)
                    .timeout(Duration::from_secs(2))
                    .send()
                    .await
                    .ok()
                    .filter(|response| response.status().is_success())
                {
                    Some(response) => response
                        .json::<serde_json::Value>()
                        .await
                        .ok()
                        .and_then(|value| value.get("delivered").and_then(|item| item.as_bool()))
                        .unwrap_or(false),
                    None => false,
                }
            }
        }
        NavigationIntent::Workspaces => {
            let destination = match url_with_segments(&runtime, &["settings", "workspaces"]) {
                Ok(destination) => destination,
                Err(_) => return,
            };
            // Navigating is only meaningful once the engine is up: on a cold
            // start the main window shows the bundled recovery page, so
            // confirming the WebKit navigation would clear the intent before
            // the PWA is reachable. Keep it pending until the engine answers so
            // the runtime watcher retries and lands on the route.
            if !engine_reachable(&runtime) {
                return;
            }
            main.navigate(destination).is_ok()
        }
        NavigationIntent::Notifications => {
            let destination = match url_with_segments(&runtime, &["settings", "notifications"]) {
                Ok(destination) => destination,
                Err(_) => return,
            };
            if !engine_reachable(&runtime) {
                return;
            }
            main.navigate(destination).is_ok()
        }
    };
    if applied
        && let Ok(mut pending) = app.state::<DesktopModel>().pending_navigation.lock()
        && pending.as_ref() == Some(&intent)
    {
        *pending = None;
    }
}

fn queue_navigation(app: &AppHandle, intent: NavigationIntent) {
    if let Ok(mut pending) = app.state::<DesktopModel>().pending_navigation.lock() {
        *pending = Some(intent);
    }
    show_window(app, "main");
    tauri::async_runtime::spawn(try_apply_pending_navigation(app.clone()));
}

fn setup_url(binary: &std::path::Path, runtime: &RuntimeConfig) -> Option<url::Url> {
    let workspace = runtime.workspace.as_ref()?;
    let output = Command::new(binary)
        .arg("setup-url")
        .arg("--workspace")
        .arg(workspace)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8_lossy(&output.stdout)
        .split_whitespace()
        .find_map(|word| url::Url::parse(word.trim()).ok())
}

fn should_show_main_window(background_launch: bool, server_configured: bool) -> bool {
    !background_launch || !server_configured
}

fn engine_reachable(runtime: &RuntimeConfig) -> bool {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, runtime.port));
    TcpStream::connect_timeout(&address, Duration::from_millis(250)).is_ok()
}

fn current_app_bundle() -> Option<std::path::PathBuf> {
    env::current_exe().ok()?.ancestors().find_map(|path| {
        (path.extension().and_then(|value| value.to_str()) == Some("app"))
            .then(|| path.to_path_buf())
    })
}

fn build_windows(
    app: &AppHandle,
    runtime: &RuntimeConfig,
    settings: &mut DesktopSettings,
    store: &SettingsStore,
) -> tauri::Result<Arc<RwLock<url::Url>>> {
    let binary = service::resolve_ciao(env::var("PATH").ok().as_deref());
    let server_configured = runtime.server_plist.is_file();
    // A discarded Result here is indistinguishable from never having tried, so
    // a cold start that never brings up the engine leaves no evidence anywhere.
    if !server_configured {
        match binary.as_deref() {
            Some(binary) => {
                if let Err(error) = service::spawn_bootstrap(binary, runtime.workspace.as_deref()) {
                    append_log(&runtime.runtime_root, &format!("bootstrap FAILED: {error}"));
                }
            }
            None => append_log(
                &runtime.runtime_root,
                "bootstrap FAILED: the ciao executable was not found",
            ),
        }
    }
    let reachable = engine_reachable(runtime);
    let mut main_url = runtime.server_url.clone();
    if reachable
        && !settings.auth_bootstrapped
        && let Some(binary) = binary.as_deref()
        && let Some(url) = setup_url(binary, runtime)
    {
        main_url = url;
        settings.auth_bootstrapped = true;
        let _ = store.save(settings);
    }
    let current_main_url = Arc::new(RwLock::new(main_url.clone()));
    let trusted_server_url = runtime.server_url.clone();
    let initial_url = if reachable {
        WebviewUrl::External(main_url)
    } else {
        WebviewUrl::App("startup.html".into())
    };
    let main = WebviewWindowBuilder::new(app, "main", initial_url)
        .title("Ciaobot")
        .inner_size(1180.0, 780.0)
        .min_inner_size(760.0, 560.0)
        // Keep Tauri's native Finder-drop handler enabled: WKWebView strips
        // absolute paths from HTML File objects. The event is bridged below
        // as a short-lived grant, without exposing a general filesystem IPC
        // capability to the remotely loaded PWA.
        .on_navigation({
            let current_main_url = Arc::clone(&current_main_url);
            move |url| {
                if !is_trusted_main_navigation(url, &trusted_server_url) {
                    if is_external_link(url) {
                        let _ = Command::new("open").arg(url.as_str()).spawn();
                    }
                    return false;
                }
                if let Ok(mut current) = current_main_url.write() {
                    *current = url.clone();
                }
                true
            }
        })
        // Lets the remotely loaded PWA tell it is running inside the app so it
        // can drop surfaces the tray owns (package update, native
        // notifications). A one-way marker, not an IPC channel: the main
        // webview stays out of every capability.
        .initialization_script("window.__CIAOBOT_DESKTOP__ = true;")
        // The PWA marks outbound links target="_blank" and also calls
        // window.open. WKWebView asks the host to build the new webview for
        // those, and with no handler installed the click silently does
        // nothing, so hand them to the system browser instead.
        .on_new_window(|url, _features| {
            if is_external_link(&url) {
                let _ = Command::new("open").arg(url.as_str()).spawn();
            }
            NewWindowResponse::Deny
        })
        .visible(should_show_main_window(
            env::args_os().any(|argument| argument == "--background"),
            server_configured,
        ))
        .build()?;
    main.on_window_event({
        let window = main.clone();
        let runtime_root = runtime.runtime_root.clone();
        move |event| {
            match event {
                WindowEvent::DragDrop(DragDropEvent::Enter { .. }) => {
                    let _ = window.eval(browser_event_script(
                        "ciao:native-file-drag-enter",
                        &serde_json::json!({}),
                    ));
                }
                WindowEvent::DragDrop(DragDropEvent::Leave) => {
                    let _ = window.eval(browser_event_script(
                        "ciao:native-file-drag-leave",
                        &serde_json::json!({}),
                    ));
                }
                WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => {
                    // Off the window event thread, because building the grant
                    // stages files and staging a cloud placeholder has to
                    // materialise it first — a download, not a copy (see
                    // `needs_drop_staging`). Run inline, that is the whole window
                    // frozen until iCloud answers. Clear the drag highlight now
                    // rather than leaving it lit for that wait; the page clears it
                    // again when the grant lands.
                    let _ = window.eval(browser_event_script(
                        "ciao:native-file-drag-leave",
                        &serde_json::json!({}),
                    ));
                    let window = window.clone();
                    let runtime_root = runtime_root.clone();
                    let paths = paths.clone();
                    thread::spawn(move || {
                        let detail = match create_desktop_drop_grant(&runtime_root, &paths) {
                            Ok((grant_id, paths)) => serde_json::json!({
                                "grantId": grant_id,
                                "paths": paths,
                            }),
                            Err(error) => serde_json::json!({ "error": error }),
                        };
                        let _ = window.eval(browser_event_script("ciao:native-file-drop", &detail));
                    });
                }
                WindowEvent::CloseRequested { api, .. } => {
                    // Closing the window leaves Ciaobot running in the menu bar;
                    // quitting is a tray action. Drop the Dock tile with it,
                    // unless the user unchecked Hide Dock Icon.
                    api.prevent_close();
                    let _ = window.hide();
                    hide_dock_unless_pinned(&window.app_handle().clone());
                }
                _ => {}
            }
        }
    });

    // Keep a native startup-style window ready for updates. The main webview
    // loads the remote PWA, while this local page remains available even when
    // the engine is being replaced or the PWA is no longer reachable.
    let update = WebviewWindowBuilder::new(app, "update", WebviewUrl::App("startup.html".into()))
        .title("Updating Ciaobot")
        .inner_size(1180.0, 780.0)
        .min_inner_size(760.0, 560.0)
        .visible(false)
        .build()?;
    update.on_window_event({
        let window = update.clone();
        move |event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // The update cannot be cancelled safely after the bundle swap
                // begins. Closing the window only hides the progress surface;
                // the tray operation continues and the app will restart.
                api.prevent_close();
                let _ = window.hide();
            }
        }
    });
    Ok(current_main_url)
}

// The tray rebuild runs on a background thread and its Result was being
// discarded, so a failure to build or update the menu looked identical to
// "nothing happened". Record those in the workspace runtime dir instead.
// Takes the runtime root rather than the AppHandle so the startup path can use
// it too: build_windows runs before DesktopModel is managed, and `tray_log`'s
// `app.state::<DesktopModel>()` would panic there.
fn append_log(runtime_root: &std::path::Path, message: &str) {
    let path = runtime_root.join("desktop-tray.log");
    if let Ok(metadata) = std::fs::metadata(&path)
        && metadata.len() > 256 * 1024
    {
        let _ = std::fs::remove_file(&path);
    }
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        use std::io::Write;
        let _ = writeln!(file, "{message}");
    }
}

fn tray_log(app: &AppHandle, message: &str) {
    let model = app.state::<DesktopModel>();
    let Ok(runtime) = model.runtime.read() else {
        return;
    };
    append_log(&runtime.runtime_root, message);
}

fn show_error(app: &AppHandle, title: &str, message: impl Into<String>) {
    app.dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Error)
        .show(|_| {});
}

// Same dialog, but it does not return until the user dismisses it. For the one
// caller that is about to end the process: `app.restart()` tears everything
// down before a `.show()` callback could paint, so the user would never see it.
// Only safe off the main thread.
fn show_error_blocking(app: &AppHandle, title: &str, message: impl Into<String>) {
    app.dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Error)
        .blocking_show();
}

fn show_info(app: &AppHandle, title: &str, message: impl Into<String>) {
    app.dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Info)
        .show(|_| {});
}

fn invoke_service_action(app: AppHandle, action: &'static str, force: bool, exit: bool) {
    thread::spawn(move || {
        let Some(binary) = service::resolve_ciao(env::var("PATH").ok().as_deref()) else {
            show_error(
                &app,
                "Ciaobot engine unavailable",
                "The ciao executable was not found.",
            );
            return;
        };
        let extra = if force { &["--force"][..] } else { &[][..] };
        match service::invoke(&binary, action, extra) {
            Ok(result) if result.ok => {
                if exit {
                    app.exit(0);
                }
            }
            Ok(result)
                if !force
                    && result
                        .details
                        .get("requires_confirmation")
                        .and_then(serde_json::Value::as_bool)
                        .unwrap_or(false) =>
            {
                let count = result
                    .details
                    .get("active_chat_ids")
                    .and_then(serde_json::Value::as_array)
                    .map_or(0, Vec::len);
                let app_for_dialog = app.clone();
                app.dialog()
                    .message(format!(
                        "{count} active chat{} will be interrupted.",
                        if count == 1 { "" } else { "s" }
                    ))
                    .title(if action == "restart" {
                        "Restart Ciaobot engine?"
                    } else {
                        "Stop Ciaobot engine?"
                    })
                    .buttons(MessageDialogButtons::OkCancelCustom(
                        if action == "restart" {
                            "Restart".into()
                        } else {
                            "Stop".into()
                        },
                        "Cancel".into(),
                    ))
                    .show(move |confirmed| {
                        if confirmed {
                            invoke_service_action(app_for_dialog, action, true, exit);
                        }
                    });
            }
            Ok(result) => show_error(&app, "Engine action failed", result.message),
            Err(error) => show_error(&app, "Engine action failed", error),
        }
    });
}

fn disconnect_from_host(app: AppHandle) {
    thread::spawn(move || {
        let runtime = match app.state::<DesktopModel>().runtime.read() {
            Ok(runtime) => runtime.clone(),
            Err(error) => {
                show_error(&app, "Could not disconnect from host", error.to_string());
                return;
            }
        };
        let result = tauri::async_runtime::block_on(capture::disconnect_from_host(&runtime));
        match result {
            Ok(()) => {
                tray_log(&app, "client disconnect: promoted local node to host");
                if let Some(main) = app.get_webview_window("main") {
                    let _ = main.navigate(runtime.server_url);
                }
                let _ = refresh_tray(&app);
            }
            Err(error) => show_error(&app, "Could not disconnect from host", error),
        }
    });
}

fn prompt_disconnect_from_host(app: &AppHandle) {
    let app_for_action = app.clone();
    app.dialog()
        .message("The host may be unavailable. Disconnect this device and make it the host here?")
        .title("Disconnect from host?")
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Disconnect".into(),
            "Cancel".into(),
        ))
        .show(move |confirmed| {
            if confirmed {
                disconnect_from_host(app_for_action);
            }
        });
}

fn set_login_enabled(app: &AppHandle, enabled: bool) -> Result<(), String> {
    let previous = app
        .autolaunch()
        .is_enabled()
        .map_err(|error| error.to_string())?;
    let binary = service::resolve_ciao(env::var("PATH").ok().as_deref())
        .ok_or_else(|| "The ciao engine executable was not found.".to_string())?;
    let action = if enabled { "enable" } else { "disable" };
    let result = service::invoke(&binary, "login", &[action])?;
    if !result.ok {
        return Err(result.message);
    }
    let app_result = if enabled {
        app.autolaunch().enable()
    } else {
        app.autolaunch().disable()
    };
    if let Err(error) = app_result {
        let rollback = if previous { "enable" } else { "disable" };
        let _ = service::invoke(&binary, "login", &[rollback]);
        return Err(format!("Could not update app login launch: {error}"));
    }
    Ok(())
}

fn refresh_tray(app: &AppHandle) -> Result<(), String> {
    let model = app.state::<DesktopModel>();
    let snapshot = model
        .tray_snapshot
        .read()
        .map_err(|error| error.to_string())?
        .clone();
    let (notifications, hide_dock_icon) = {
        let settings = model.settings.lock().map_err(|error| error.to_string())?;
        (settings.notifications_enabled, settings.hide_dock_icon)
    };
    let login = app.autolaunch().is_enabled().unwrap_or(false);
    let built = tray::build_menu(
        app,
        &snapshot,
        notifications,
        notification_permission_state().contains("denied"),
        login,
        hide_dock_icon,
    )
    .map_err(|e| e.to_string())?;
    let working_rows = built.working_items.len();
    tray_log(
        app,
        &format!(
            "refresh: reachable={} chats={} active={} working_rows={} attention={}",
            snapshot.reachable,
            snapshot.chats.len(),
            snapshot.active_chat_ids.len(),
            working_rows,
            snapshot.attention_count,
        ),
    );
    if let Ok(mut items) = model.working_items.lock() {
        *items = built.working_items;
    }
    if let Some(icon) = app.tray_by_id("ciaobot") {
        icon.set_menu(Some(built.menu))
            .map_err(|error| error.to_string())?;
        // Clear with an empty string, never None: tray-icon's set_title_inner
        // only calls setTitle: inside `if let Some(title)`, so passing None is a
        // no-op and the count stays on the button forever. That left a stale
        // badge beside the face with no unread chat to explain it.
        let badge = if snapshot.attention_count == 0 {
            String::new()
        } else {
            format!(" {}", snapshot.attention_count)
        };
        icon.set_title(Some(badge))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let settings = app.state::<DesktopModel>().settings.lock().unwrap().clone();
    let menu = tray::build_menu(
        app,
        &TraySnapshot::default(),
        settings.notifications_enabled,
        notification_permission_state().contains("denied"),
        app.autolaunch().is_enabled().unwrap_or(false),
        settings.hide_dock_icon,
    )?
    .menu;
    let icon = Image::from_bytes(include_bytes!(
        "../../../ciao/stock/deploy/face_template.png"
    ))?;
    TrayIconBuilder::with_id("ciaobot")
        .icon(icon)
        .icon_as_template(true)
        .menu(&menu)
        .on_menu_event(|app, event| {
            let id = event.id().as_ref();
            if let Some(chat_id) = id.strip_prefix("chat:") {
                queue_navigation(app, NavigationIntent::Chat(chat_id.to_string()));
                return;
            }
            match id {
                "open" => show_window(app, "main"),
                "disconnect" => prompt_disconnect_from_host(app),
                "start" => invoke_service_action(app.clone(), "start", false, false),
                "restart" => invoke_service_action(app.clone(), "restart", false, false),
                "update" => {
                    let app_for_dialog = app.clone();
                    app.dialog()
                        .message(
                            "Ciaobot will update its engine and app, then restart. Active work will be interrupted.",
                        )
                        .title("Update Ciaobot?")
                        .buttons(MessageDialogButtons::OkCancelCustom(
                            "Update".into(),
                            "Cancel".into(),
                        ))
                        .show(move |confirmed| {
                            if confirmed {
                                run_full_update(app_for_dialog, false);
                            }
                        });
                }
                "notifications" => {
                    let model = app.state::<DesktopModel>();
                    let mut enabled = false;
                    if let Ok(mut settings) = model.settings.lock() {
                        settings.notifications_enabled = !settings.notifications_enabled;
                        enabled = settings.notifications_enabled;
                        if let Err(error) = model.store.save(&settings) {
                            show_error(
                                app,
                                "Could not save notification setting",
                                error.to_string(),
                            );
                        }
                    }
                    // The bundled settings window used to own the permission
                    // prompt, so turning the toggle on has to ask for it now.
                    maybe_request_notification_permission(app, enabled);
                    let _ = refresh_tray(app);
                }
                "notification-settings" => {
                    queue_navigation(app, NavigationIntent::Notifications);
                }
                "hide-dock-icon" => {
                    let model = app.state::<DesktopModel>();
                    let mut hide = true;
                    if let Ok(mut settings) = model.settings.lock() {
                        settings.hide_dock_icon = !settings.hide_dock_icon;
                        hide = settings.hide_dock_icon;
                        if let Err(error) = model.store.save(&settings) {
                            show_error(app, "Could not save Dock setting", error.to_string());
                        }
                    }
                    // Apply immediately rather than at the next window close, so
                    // the checkbox visibly does something: unchecking it while
                    // no window is open should bring the tile back right away.
                    let showing = app
                        .webview_windows()
                        .values()
                        .any(|window| window.is_visible().unwrap_or(false));
                    set_dock_visible(app, showing || !hide);
                    let _ = refresh_tray(app);
                }
                "start-at-login" => {
                    let app = app.clone();
                    thread::spawn(move || {
                        let enabled = app.autolaunch().is_enabled().unwrap_or(false);
                        if let Err(error) = set_login_enabled(&app, !enabled) {
                            show_error(&app, "Could not update login launch", error);
                        }
                        let _ = refresh_tray(&app);
                    });
                }
                "logs" => {
                    let runtime = app
                        .state::<DesktopModel>()
                        .runtime
                        .read()
                        .ok()
                        .map(|v| v.clone());
                    if let Some(runtime) = runtime {
                        let _ = Command::new("open")
                            .arg(runtime.runtime_root.join("ciao.stderr.log"))
                            .spawn();
                    }
                }
                "github" => {
                    let _ = Command::new("open")
                        .arg("https://github.com/raffaelefarinaro/ciaobot")
                        .spawn();
                }
                "report-issue" => {
                    let _ = Command::new("open")
                        .arg("https://github.com/raffaelefarinaro/ciaobot/issues/new")
                        .spawn();
                }
                "quit" => {
                    let app_for_dialog = app.clone();
                    app.dialog()
                        .message(
                            "This stops the engine, scheduled tasks, and any active agent work.",
                        )
                        .title("Quit Ciaobot?")
                        .buttons(MessageDialogButtons::OkCancelCustom(
                            "Quit".into(),
                            "Cancel".into(),
                        ))
                        .show(move |confirmed| {
                            if confirmed {
                                invoke_service_action(app_for_dialog, "stop", true, true);
                            }
                        });
                }
                _ => {}
            }
        })
        .build(app)?;
    Ok(())
}

fn start_tray_watcher(app: AppHandle) {
    thread::spawn(move || {
        let mut tolerance = capture::ProbeTolerance::default();
        loop {
            let runtime = match app.state::<DesktopModel>().runtime.read() {
                Ok(runtime) => runtime.clone(),
                Err(_) => {
                    thread::sleep(Duration::from_secs(2));
                    continue;
                }
            };
            let probe = tauri::async_runtime::block_on(capture::tray_snapshot(&runtime));
            // One failed probe is not proof the engine died; keep the previous
            // snapshot until a run of them agrees.
            let Some(snapshot) = tolerance.observe(probe) else {
                thread::sleep(Duration::from_secs(2));
                continue;
            };
            let now_reachable = snapshot.reachable;
            let previously_reachable = app
                .state::<DesktopModel>()
                .tray_snapshot
                .read()
                .map(|current| current.reachable)
                .unwrap_or(false);
            let changed = app
                .state::<DesktopModel>()
                .tray_snapshot
                .read()
                .map(|current| *current != snapshot)
                .unwrap_or(true);
            if changed {
                if let Ok(mut current) = app.state::<DesktopModel>().tray_snapshot.write() {
                    *current = snapshot;
                }
                if let Err(error) = refresh_tray(&app) {
                    tray_log(&app, &format!("refresh FAILED: {error}"));
                }
            }
            if now_reachable
                && !previously_reachable
                && let Some(main) = app.get_webview_window("main")
            {
                let _ = main.navigate(runtime.server_url.clone());
            }
            thread::sleep(Duration::from_secs(2));
        }
    });
}

fn start_engine_if_needed(app: AppHandle, runtime: RuntimeConfig) {
    if engine_reachable(&runtime) || !runtime.server_plist.is_file() {
        return;
    }
    thread::spawn(move || {
        let Some(binary) = service::resolve_ciao(env::var("PATH").ok().as_deref()) else {
            // The running bundle has no resolvable engine (a repo-built
            // `target/release/bundle` app, for one), but the server plist on
            // disk still names the installed engine. Re-register it through
            // launchd instead of giving up — giving up left the splash stuck
            // on "Waiting for the engine" for the whole session.
            match service::bootstrap_existing_service(&runtime.server_plist) {
                Ok(()) => tray_log(&app, "engine start: re-registered the existing LaunchAgent"),
                Err(error) => tray_log(&app, &format!("engine start FAILED: {error}")),
            }
            let _ = refresh_tray(&app);
            return;
        };
        // Same reason as the bootstrap path: without this, an engine that fails
        // to start looks exactly like an engine that was never asked to.
        match service::invoke(&binary, "start", &[]) {
            Ok(result) if result.ok => tray_log(&app, "engine start: ok"),
            Ok(result) => tray_log(&app, &format!("engine start FAILED: {}", result.message)),
            Err(error) => tray_log(&app, &format!("engine start FAILED: {error}")),
        }
        let _ = refresh_tray(&app);
    });
}

fn start_tray_icon_animation(app: AppHandle) {
    thread::spawn(move || {
        let mut frame = 0usize;
        // The head spin has 12 frames and the dot pulse 8, so they need
        // separate counters or the shorter ramp restarts mid-cycle and stutters.
        let mut dot_frame = 0usize;
        let mut dot_error_logged = false;
        let mut last_key = None;
        loop {
            thread::sleep(Duration::from_millis(120));
            let snapshot = match app.state::<DesktopModel>().tray_snapshot.read() {
                Ok(snapshot) => snapshot.clone(),
                Err(_) => continue,
            };
            let working = !snapshot.active_chat_ids.is_empty();
            let key = (
                snapshot.reachable,
                working,
                if working { frame } else { 0 },
                if working { dot_frame } else { 0 },
            );
            if last_key == Some(key) {
                continue;
            }
            let bytes: &[u8] = if !snapshot.reachable {
                include_bytes!("../../../ciao/stock/deploy/face_scared_template.png")
            } else if working {
                match frame % 12 {
                    0 => include_bytes!("../../../ciao/stock/deploy/face_spin_00.png"),
                    1 => include_bytes!("../../../ciao/stock/deploy/face_spin_01.png"),
                    2 => include_bytes!("../../../ciao/stock/deploy/face_spin_02.png"),
                    3 => include_bytes!("../../../ciao/stock/deploy/face_spin_03.png"),
                    4 => include_bytes!("../../../ciao/stock/deploy/face_spin_04.png"),
                    5 => include_bytes!("../../../ciao/stock/deploy/face_spin_05.png"),
                    6 => include_bytes!("../../../ciao/stock/deploy/face_spin_06.png"),
                    7 => include_bytes!("../../../ciao/stock/deploy/face_spin_07.png"),
                    8 => include_bytes!("../../../ciao/stock/deploy/face_spin_08.png"),
                    9 => include_bytes!("../../../ciao/stock/deploy/face_spin_09.png"),
                    10 => include_bytes!("../../../ciao/stock/deploy/face_spin_10.png"),
                    _ => include_bytes!("../../../ciao/stock/deploy/face_spin_11.png"),
                }
            } else {
                include_bytes!("../../../ciao/stock/deploy/face_template.png")
            };
            // tray-icon's set_icon installs the image with is_template hardcoded
            // to false, discarding the stored flag, so it has to be re-marked
            // after every swap. Both calls have to land in the *same* main-thread
            // turn: on their own each hops the main thread separately, the
            // compositor draws in between, and these frames are pure black — so
            // the spinning head flashed black on every frame.
            let icon_app = app.clone();
            let _ = app.run_on_main_thread(move || {
                if let Ok(image) = Image::from_bytes(bytes)
                    && let Some(icon) = icon_app.tray_by_id("ciaobot")
                {
                    let _ = icon.set_icon(Some(image));
                    let _ = icon.set_icon_as_template(true);
                }
            });
            // Re-render the pulse on each working chat row. The rows are
            // retained by refresh_tray, so this rewrites just their text
            // instead of rebuilding the menu (which would fight the user
            // having it open).
            if working
                && let Ok(items) = app.state::<DesktopModel>().working_items.lock()
                && !items.is_empty()
            {
                let glyph = tray::working_pulse_glyph(dot_frame);
                for row in items.iter() {
                    if let Err(error) = row.item.set_text(format!("{glyph} {}", row.label))
                        && !dot_error_logged
                    {
                        dot_error_logged = true;
                        tray_log(&app, &format!("set_text FAILED: {error}"));
                    }
                }
            }
            last_key = Some(key);
            if working {
                frame = (frame + 1) % 12;
                dot_frame = (dot_frame + 1) % tray::WORKING_PULSE_FRAMES;
            }
        }
    });
}

fn start_notification_tail(app: AppHandle, runtime: RuntimeConfig) {
    let path = runtime.runtime_root.join("notifications.jsonl");
    thread::spawn(move || {
        let mut tail = NotificationLogTail::at_end(path);
        loop {
            thread::sleep(Duration::from_secs(1));
            let still_current = app
                .state::<DesktopModel>()
                .runtime
                .read()
                .map(|current| current.runtime_root == runtime.runtime_root)
                .unwrap_or(false);
            if !still_current {
                return;
            }
            // Poll even when the toggle is off, so the cursor keeps moving and
            // turning notifications back on does not dump the whole backlog.
            let fetched =
                tauri::async_runtime::block_on(capture::notifications(&runtime, tail.cursor()))
                    .ok();
            let pending = tail.poll(fetched);
            let enabled = app
                .state::<DesktopModel>()
                .settings
                .lock()
                .map(|settings| settings.notifications_enabled)
                .unwrap_or(false);
            for payload in pending {
                if payload.get("kind").and_then(serde_json::Value::as_str) == Some("clear") {
                    let chat_id = payload
                        .get("chat_id")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    tauri::async_runtime::spawn(async move {
                        native_notifications::dismiss_chat_notifications(&chat_id).await;
                    });
                    continue;
                }
                if !enabled {
                    continue;
                }
                let notification = NativeNotification::from_value(&payload);
                tauri::async_runtime::spawn(async move {
                    let _ = notification.post().await;
                });
            }
        }
    });
}

fn start_runtime_watcher(app: AppHandle) {
    thread::spawn(move || {
        loop {
            thread::sleep(Duration::from_secs(2));
            let discovered = runtime::discover_current();
            let previous = match app.state::<DesktopModel>().runtime.read() {
                Ok(runtime) => runtime.clone(),
                Err(_) => continue,
            };
            if discovered != previous {
                let url_changed = discovered.server_url != previous.server_url;
                let root_changed = discovered.runtime_root != previous.runtime_root;
                if let Ok(mut current) = app.state::<DesktopModel>().runtime.write() {
                    *current = discovered.clone();
                }
                if url_changed && let Some(main) = app.get_webview_window("main") {
                    let _ = main.navigate(discovered.server_url.clone());
                }
                if root_changed {
                    start_notification_tail(app.clone(), discovered);
                }
            }
            tauri::async_runtime::spawn(try_apply_pending_navigation(app.clone()));
        }
    });
}

#[tauri::command]
fn check_permission(kind: permissions::PermissionKind) -> permissions::PermissionState {
    permissions::query(kind)
}

// Async on purpose: a synchronous command would run the permission prompt's
// blocking wait on the main thread and freeze every window and the tray until
// the user answered it.
#[tauri::command]
async fn request_permission(kind: permissions::PermissionKind) -> permissions::PermissionState {
    permissions::request_async(kind).await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            check_permission,
            request_permission
        ])
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_window(app, "main");
            tauri::async_runtime::spawn(try_apply_pending_navigation(app.clone()));
        }))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        // The release installer and the tray must own the same LaunchAgent.
        // Explicitly name it so the plugin does not derive
        // `ciaobot-desktop.plist` from the Rust package name while the
        // installer manages `Ciaobot.plist`.
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .app_name("Ciaobot")
                .args(["--background"])
                .macos_launcher(MacosLauncher::LaunchAgent)
                .build(),
        )
        .setup(|app| {
            let runtime = runtime::discover_current();
            let app_data = app.path().app_data_dir()?;
            let store = SettingsStore::new(&app_data);
            let mut settings = store.load(Some(&runtime.runtime_root.join("menubar_prefs.json")));
            let main_url = build_windows(app.handle(), &runtime, &mut settings, &store)?;
            app.manage(DesktopModel {
                runtime: RwLock::new(runtime.clone()),
                settings: Mutex::new(settings.clone()),
                store,
                pending_navigation: Mutex::new(None),
                tray_snapshot: RwLock::new(TraySnapshot::default()),
                main_url,
                working_items: Mutex::new(Vec::new()),
            });
            install_action_listener({
                let app = app.handle().clone();
                move |intent| queue_navigation(&app, intent)
            });
            build_tray(app.handle())?;
            start_tray_watcher(app.handle().clone());
            start_tray_icon_animation(app.handle().clone());
            start_engine_if_needed(app.handle().clone(), runtime.clone());
            maybe_request_notification_permission(app.handle(), settings.notifications_enabled);
            let migration =
                service::resolve_ciao(env::var("PATH").ok().as_deref()).and_then(|binary| {
                    current_app_bundle().and_then(|bundle| {
                        bundle.to_str().and_then(|path| {
                            service::invoke(&binary, "migrate", &["--app-bundle", path]).ok()
                        })
                    })
                });
            if let Some(migration) = migration.filter(|result| result.ok) {
                maybe_show_browser_pwa_notice(app.handle(), &migration);
            }
            // Native notifications are independent of legacy migration. A
            // failed/missing migration helper must not disable them.
            start_notification_tail(app.handle().clone(), runtime);
            start_runtime_watcher(app.handle().clone());
            let main_visible = app
                .handle()
                .get_webview_window("main")
                .and_then(|window| window.is_visible().ok())
                .unwrap_or(false);
            // With Hide Dock Icon off, the tile stays put even on a windowless
            // menu-bar-only launch.
            let keep_dock = !hide_dock_icon_enabled(app.handle());
            set_dock_visible(app.handle(), main_visible || keep_dock);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Ciaobot")
        .run(|app, event| {
            // Clicking the Dock tile (or picking Ciaobot in the app switcher)
            // should always surface the window, including when it is hidden.
            if let tauri::RunEvent::Reopen { .. } = event {
                show_window(app, "main");
            }
        });
}

#[cfg(test)]
mod tests {
    use super::{
        append_log, browser_event_script, create_desktop_drop_grant, download_percent,
        engine_already_current, engine_launch_action, flags_are_dataless, is_external_link,
        is_trusted_main_navigation, needs_drop_staging, requires_confirmation,
        should_show_main_window, should_stage,
    };
    use crate::service::ServiceResult;

    fn result_with(details: serde_json::Value) -> ServiceResult {
        ServiceResult {
            ok: false,
            action: "update-engine".into(),
            message: "…".into(),
            details,
        }
    }

    // The startup paths log through append_log directly because DesktopModel is
    // not managed yet, so it has to work from a runtime root alone.
    #[test]
    fn append_log_appends_to_the_runtime_root_and_drops_an_oversized_log() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("desktop-tray.log");

        append_log(root.path(), "engine start: ok");
        append_log(root.path(), "engine start FAILED: nope");
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            "engine start: ok\nengine start FAILED: nope\n"
        );

        std::fs::write(&path, vec![b'x'; 256 * 1024 + 1]).unwrap();
        append_log(root.path(), "after the cap");
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            "after the cap\n",
            "an oversized log should be dropped, not appended to forever"
        );
    }

    #[test]
    fn background_login_hides_configured_app_but_not_first_run_setup() {
        assert!(!should_show_main_window(true, true));
        assert!(should_show_main_window(true, false));
        assert!(should_show_main_window(false, true));
    }

    #[test]
    fn only_browser_schemes_reach_the_system_opener() {
        for allowed in [
            "https://example.com/doc",
            "http://example.com/doc",
            "mailto:someone@example.com",
        ] {
            assert!(
                is_external_link(&url::Url::parse(allowed).unwrap()),
                "{allowed} should open externally"
            );
        }
        for blocked in [
            "file:///Users/someone/.ssh/id_rsa",
            "javascript:alert(1)",
            "smb://server/share",
        ] {
            assert!(
                !is_external_link(&url::Url::parse(blocked).unwrap()),
                "{blocked} must not be handed to the opener"
            );
        }
    }

    #[test]
    fn main_webview_stays_on_the_local_engine_or_bundled_origin() {
        let server = url::Url::parse("http://localhost:8443/").unwrap();
        for allowed in [
            "http://localhost:8443/chat/example",
            "tauri://localhost/startup.html",
            "http://tauri.localhost/startup.html",
        ] {
            assert!(
                is_trusted_main_navigation(&url::Url::parse(allowed).unwrap(), &server),
                "{allowed} should stay in the main webview"
            );
        }
        for blocked in [
            "https://example.com/",
            "http://localhost:8444/",
            "file:///Users/someone/.ssh/id_rsa",
        ] {
            assert!(
                !is_trusted_main_navigation(&url::Url::parse(blocked).unwrap(), &server),
                "{blocked} must not replace the trusted PWA"
            );
        }
    }

    // A drop the server cannot read itself gets copied: images from the
    // NSIRD screenshot dir (permissions) and dataless File Provider files of
    // any type (the server's read would deadlock). A readable local
    // screenshot, and a non-image inside an NSIRD dir, both stay
    // pass-through: the server can read the first, and the agent needs to
    // keep reading the second.
    #[test]
    fn only_an_unreadable_drop_needs_staging() {
        let temporary =
            std::path::Path::new("/var/folders/zj/x/T/TemporaryItems/NSIRD_screencaptureui_1Pr326");
        assert!(needs_drop_staging(
            &temporary.join("Screenshot 2026-07-31 at 09.54.13.png")
        ));
        assert!(needs_drop_staging(&temporary.join("SHOT.JPEG")));
        assert!(!needs_drop_staging(&temporary.join("notes.pdf")));

        // An ordinary local image, on disk and materialised. Written for real
        // rather than named: the dataless check stats the path, so a bare
        // nonexistent path would pass this assertion without exercising it.
        let root = tempfile::tempdir().unwrap();
        let local = root.path().join("Screenshot.png");
        std::fs::write(&local, b"\x89PNG\r\n\x1a\n").unwrap();
        assert!(!needs_drop_staging(&local));
    }

    // The staging decision itself, covering the branches a temp dir cannot:
    // a dataless PDF must be staged just like a dataless image, while an
    // NSIRD non-image stays pass-through so the agent keeps reading it.
    #[test]
    fn staging_decision_covers_dataless_non_images() {
        assert!(should_stage(false, false, true));
        assert!(should_stage(true, false, true));
        assert!(!should_stage(true, false, false));
        assert!(should_stage(true, true, false));
        assert!(!should_stage(false, true, false));
    }

    // The bit test behind the dataless check. macOS sets SF_DATALESS (0x40000000)
    // on an iCloud file whose bytes are not on disk; the flags below are real
    // values read off this machine, materialised and not.
    #[test]
    fn dataless_flags_are_recognised() {
        assert!(flags_are_dataless(0x4000_0060));
        assert!(flags_are_dataless(0x4000_0000));
        assert!(!flags_are_dataless(0x0000_0060));
        assert!(!flags_are_dataless(0));
    }

    #[test]
    fn a_staged_drop_hands_the_server_a_readable_copy() {
        let root = tempfile::tempdir().unwrap();
        let source_dir = root
            .path()
            .join("TemporaryItems/NSIRD_screencaptureui_1Pr326");
        std::fs::create_dir_all(&source_dir).unwrap();
        let source = source_dir.join("Screenshot.png");
        std::fs::write(&source, b"\x89PNG\r\n\x1a\n").unwrap();
        let plain = root.path().join("notes.md");
        std::fs::write(&plain, b"notes").unwrap();

        let (grant_id, paths) =
            create_desktop_drop_grant(root.path(), &[source.clone(), plain.clone()]).unwrap();

        // The screenshot now points at the staged copy, byte-identical and no
        // longer inside the directory only this process may read.
        let staged = std::path::PathBuf::from(&paths[0]);
        assert_ne!(staged, source);
        assert!(
            staged.starts_with(
                root.path()
                    .join("desktop-drop-grants/staged")
                    .join(&grant_id)
            )
        );
        assert_eq!(std::fs::read(&staged).unwrap(), b"\x89PNG\r\n\x1a\n");
        // The ordinary file is untouched.
        assert_eq!(paths[1], plain.to_string_lossy());
    }

    #[test]
    fn native_drop_grant_preserves_paths_and_escapes_browser_event_data() {
        let root = tempfile::tempdir().unwrap();
        let paths = vec![
            root.path().join("file with spaces.md"),
            root.path().join("quote-'\".txt"),
        ];

        let (grant_id, serialized_paths) = create_desktop_drop_grant(root.path(), &paths).unwrap();
        let grant_path = root
            .path()
            .join("desktop-drop-grants")
            .join(format!("{grant_id}.json"));
        let payload: serde_json::Value =
            serde_json::from_slice(&std::fs::read(grant_path).unwrap()).unwrap();

        assert_eq!(payload["paths"], serde_json::json!(serialized_paths));
        let script = browser_event_script(
            "ciao:native-file-drop",
            &serde_json::json!({ "paths": serialized_paths }),
        );
        assert!(
            script.starts_with("window.dispatchEvent(new CustomEvent(\"ciao:native-file-drop\"")
        );
        assert!(script.contains("\\\""));
    }

    // A no-op engine upgrade must not abort the app half of the update, and a
    // genuine engine failure must not be mistaken for one.
    #[test]
    fn a_noop_engine_upgrade_is_told_apart_from_a_real_failure() {
        assert!(engine_already_current(&result_with(
            serde_json::json!({"already_current": true})
        )));
        assert!(!engine_already_current(&result_with(
            serde_json::json!({"already_current": false})
        )));
        assert!(!engine_already_current(&result_with(serde_json::json!({}))));
        assert!(!engine_already_current(&result_with(
            serde_json::Value::Null
        )));
    }

    #[test]
    fn active_work_confirmation_is_read_from_the_service_result() {
        assert!(requires_confirmation(&result_with(
            serde_json::json!({"requires_confirmation": true})
        )));
        assert!(!requires_confirmation(&result_with(serde_json::json!({}))));
    }

    // The updater hands the callback each chunk's length, not a running total.
    // Summing the chunks has to walk the bar across the 60..=88 band; feeding
    // it a single chunk length left it parked at ~60% for the whole download.
    #[test]
    fn download_progress_advances_with_the_accumulated_total() {
        let total = Some(1_000u64);
        assert_eq!(download_percent(0, total), 60);
        assert_eq!(download_percent(250, total), 67);
        assert_eq!(download_percent(500, total), 74);
        assert_eq!(download_percent(1_000, total), 88);

        // One 250 KB chunk out of a 1 MB download is 60%, however many chunks
        // have already arrived; the accumulated total is what moves.
        let chunk = 250u64;
        let mut downloaded = 0u64;
        let mut seen = Vec::new();
        for _ in 0..4 {
            downloaded += chunk;
            seen.push(download_percent(downloaded, total));
        }
        assert_eq!(seen, vec![67, 74, 81, 88]);
        assert!(seen.windows(2).all(|pair| pair[0] < pair[1]));
    }

    #[test]
    fn download_progress_stays_in_band_without_a_content_length() {
        assert_eq!(download_percent(4_096, None), 70);
        assert_eq!(download_percent(4_096, Some(0)), 72);
        // A server that under-reports its own length must not push the bar
        // past the band the install steps own.
        assert_eq!(download_percent(9_999, Some(10)), 88);
    }

    #[test]
    fn app_update_starts_a_stopped_engine_instead_of_kickstarting_an_unloaded_job() {
        assert_eq!(
            engine_launch_action(&result_with(serde_json::json!({"loaded": true}))),
            "restart"
        );
        assert_eq!(
            engine_launch_action(&result_with(serde_json::json!({"loaded": false}))),
            "start"
        );
    }
}
