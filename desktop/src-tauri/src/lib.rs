mod capture;
mod native_notifications;
mod notification_log;
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

fn create_desktop_drop_grant(
    runtime_root: &std::path::Path,
    paths: &[std::path::PathBuf],
) -> Result<(String, Vec<String>), String> {
    let paths = paths
        .iter()
        .map(|path| path.to_string_lossy().into_owned())
        .collect::<Vec<_>>();
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
                let _ = std::fs::remove_file(entry.path());
            }
        }
    }
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

// Second half of the unified update. There is no bundled window left to emit
// progress to, so failures surface as a dialog. Only restart when something
// actually changed — otherwise "Update…" would bounce a healthy app for nothing.
async fn install_app_update(app: AppHandle, engine_updated: bool) -> Result<(), String> {
    let updater = app.updater().map_err(|error| error.to_string())?;
    let update = updater.check().await.map_err(|error| error.to_string())?;
    match update {
        Some(update) => {
            let version = update.version.clone();
            update
                .download_and_install(|_, _| {}, || {})
                .await
                .map_err(|error| format!("Update {version} could not be installed: {error}"))?;
            app.restart()
        }
        // The engine moved but the app did not: restart so both halves come
        // back on the version the engine is now running.
        None if engine_updated => app.restart(),
        None => Ok(()),
    }
}

// One update for both halves: they ship from the same tag, so updating one
// without the other is what produces an opaque desktop-service mismatch.
fn run_full_update(app: AppHandle, force: bool) {
    thread::spawn(move || {
        let Some(binary) = service::resolve_ciao(env::var("PATH").ok().as_deref()) else {
            show_error(
                &app,
                "Ciaobot engine unavailable",
                "The ciao executable was not found.",
            );
            return;
        };
        for window in app.webview_windows().values() {
            let _ = window.hide();
        }
        set_dock_visible(&app, false);
        let extra = if force { &["--force"][..] } else { &[][..] };
        let engine_updated = match service::invoke(&binary, "update-engine", extra) {
            Ok(result) if result.ok => true,
            Ok(result) if engine_already_current(&result) => false,
            Ok(result) if !force && requires_confirmation(&result) => {
                show_window(&app, "main");
                prompt_forced_update(app.clone(), &result);
                return;
            }
            Ok(result) => {
                show_window(&app, "main");
                show_error(&app, "Could not update Ciaobot", result.message);
                return;
            }
            Err(error) => {
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
                    show_window(&updater_app, "main");
                    show_info(
                        &updater_app,
                        "Ciaobot is up to date",
                        "The engine and the app are both on the latest available version.",
                    );
                }
                Err(error) => {
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
                let delivered = reqwest::Client::new()
                    .get(endpoint)
                    .timeout(Duration::from_secs(2))
                    .send()
                    .await
                    .ok()
                    .filter(|response| response.status().is_success());
                match delivered {
                    Some(response) => {
                        response
                            .json::<serde_json::Value>()
                            .await
                            .ok()
                            .and_then(|value| {
                                value.get("delivered").and_then(|item| item.as_bool())
                            })
                            .unwrap_or(false)
                            || url_with_segments(&runtime, &["chat", chat_id])
                                .is_ok_and(|destination| main.navigate(destination).is_ok())
                    }
                    None => false,
                }
            }
        }
        NavigationIntent::Workspaces => {
            let destination = match url_with_segments(&runtime, &["settings", "workspaces"]) {
                Ok(destination) => destination,
                Err(_) => return,
            };
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
                    let detail = match create_desktop_drop_grant(&runtime_root, paths) {
                        Ok((grant_id, paths)) => serde_json::json!({
                            "grantId": grant_id,
                            "paths": paths,
                        }),
                        Err(error) => serde_json::json!({ "error": error }),
                    };
                    let _ = window.eval(browser_event_script("ciao:native-file-drop", &detail));
                }
                WindowEvent::CloseRequested { api, .. } => {
                    // Closing the window leaves Ciaobot running in the menu bar;
                    // quitting is a tray action. Drop the Dock tile with it.
                    api.prevent_close();
                    let _ = window.hide();
                    set_dock_visible(&window.app_handle().clone(), false);
                }
                _ => {}
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
    let notifications = model
        .settings
        .lock()
        .map_err(|error| error.to_string())?
        .notifications_enabled;
    let login = app.autolaunch().is_enabled().unwrap_or(false);
    let built = tray::build_menu(
        app,
        &snapshot,
        notifications,
        notification_permission_state().contains("denied"),
        login,
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
            tray_log(
                &app,
                "engine start FAILED: the ciao executable was not found",
            );
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
            if !enabled {
                continue;
            }
            for payload in pending {
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_window(app, "main");
            tauri::async_runtime::spawn(try_apply_pending_navigation(app.clone()));
        }))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--background"]),
        ))
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
            set_dock_visible(app.handle(), main_visible);
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
        append_log, browser_event_script, create_desktop_drop_grant, engine_already_current,
        is_external_link, is_trusted_main_navigation, requires_confirmation,
        should_show_main_window,
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
}
