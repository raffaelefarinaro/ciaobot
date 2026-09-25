use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum PermissionKind {
    Notifications,
    Camera,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PermissionState {
    NotDetermined,
    Restricted,
    Denied,
    Authorized,
}

fn notification_status() -> PermissionState {
    let state = mac_usernotifications::blocking::get_notification_settings()
        .map(|settings| format!("{:?}", settings.authorization_status).to_lowercase())
        .unwrap_or_else(|_| "unavailable".into());
    if state.contains("authorized") {
        PermissionState::Authorized
    } else if state.contains("denied") {
        PermissionState::Denied
    } else if state.contains("restricted") {
        PermissionState::Restricted
    } else {
        PermissionState::NotDetermined
    }
}

fn request_notification_permission() -> PermissionState {
    let _ = mac_usernotifications::blocking::request_auth();
    notification_status()
}

pub fn query(kind: PermissionKind) -> PermissionState {
    match kind {
        PermissionKind::Notifications => notification_status(),
        PermissionKind::Camera => PermissionState::NotDetermined,
    }
}

fn request(kind: PermissionKind) -> PermissionState {
    match kind {
        PermissionKind::Notifications => request_notification_permission(),
        PermissionKind::Camera => PermissionState::NotDetermined,
    }
}

// Every request path here ends in a blocking wait for the user to answer a TCC
// prompt, and there is no upper bound on how long that takes. Run it on the
// thread that called it and the app is dead for the duration: on macOS a
// synchronous `#[tauri::command]` runs on the main thread, so every window and
// the tray froze -- and the system prompt can appear *behind* the frozen
// window, leaving no obvious way to unstick it. Hand the wait to a blocking
// worker so the caller can await it while the UI keeps running.
async fn request_off_caller_thread<F>(request: F) -> PermissionState
where
    F: FnOnce() -> PermissionState + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(request)
        .await
        .unwrap_or(PermissionState::NotDetermined)
}

pub async fn request_async(kind: PermissionKind) -> PermissionState {
    request_off_caller_thread(move || request(kind)).await
}

#[cfg(test)]
mod tests {
    use super::{PermissionState, request_off_caller_thread};
    use std::thread;

    // The freeze itself is not unit-testable: it needs a real main thread, a
    // real window, and a real TCC prompt that only a human can answer. What is
    // testable is the mechanism that fixes it -- the wait must not run on the
    // thread that asked for the permission.
    #[test]
    fn permission_requests_wait_off_the_calling_thread() {
        let caller = thread::current().id();
        let state = tauri::async_runtime::block_on(request_off_caller_thread(move || {
            if thread::current().id() == caller {
                PermissionState::Denied
            } else {
                PermissionState::Authorized
            }
        }));
        assert_eq!(state, PermissionState::Authorized);
    }

    // A panicking request must not take the command down with it.
    #[test]
    fn a_failed_request_reports_not_determined() {
        let state = tauri::async_runtime::block_on(request_off_caller_thread(|| {
            panic!("prompt backend unavailable")
        }));
        assert_eq!(state, PermissionState::NotDetermined);
    }
}
