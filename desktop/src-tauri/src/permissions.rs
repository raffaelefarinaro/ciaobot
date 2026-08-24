use objc2::ClassType;
use objc2_av_foundation::{AVAuthorizationStatus, AVCaptureDevice, AVMediaTypeAudio};
use serde::{Deserialize, Serialize};
use std::sync::mpsc;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum PermissionKind {
    Microphone,
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

fn av_status_to_state(status: AVAuthorizationStatus) -> PermissionState {
    if status == AVAuthorizationStatus::Authorized {
        PermissionState::Authorized
    } else if status == AVAuthorizationStatus::Denied {
        PermissionState::Denied
    } else if status == AVAuthorizationStatus::Restricted {
        PermissionState::Restricted
    } else {
        PermissionState::NotDetermined
    }
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

fn microphone_status() -> PermissionState {
    unsafe {
        let Some(media_type) = AVMediaTypeAudio else {
            return PermissionState::NotDetermined;
        };
        let cls = <AVCaptureDevice as ClassType>::class();
        let status: AVAuthorizationStatus =
            objc2::msg_send![cls, authorizationStatusForMediaType: media_type];
        av_status_to_state(status)
    }
}

fn request_microphone_permission() -> PermissionState {
    let current = microphone_status();
    if current != PermissionState::NotDetermined {
        return current;
    }
    let (tx, rx) = mpsc::channel::<bool>();
    unsafe {
        let Some(media_type) = AVMediaTypeAudio else {
            return PermissionState::NotDetermined;
        };
        let cls = <AVCaptureDevice as ClassType>::class();
        let block = block2::RcBlock::new(move |granted: objc2::runtime::Bool| {
            let _ = tx.send(bool::from(granted));
        });
        let _: () = objc2::msg_send![cls, requestAccessForMediaType: media_type, completionHandler: &*block];
    }
    match rx.recv() {
        Ok(true) => PermissionState::Authorized,
        Ok(false) => PermissionState::Denied,
        Err(_) => PermissionState::NotDetermined,
    }
}

pub fn query(kind: PermissionKind) -> PermissionState {
    match kind {
        PermissionKind::Microphone => microphone_status(),
        PermissionKind::Notifications => notification_status(),
        PermissionKind::Camera => PermissionState::NotDetermined,
    }
}

fn request(kind: PermissionKind) -> PermissionState {
    match kind {
        PermissionKind::Microphone => request_microphone_permission(),
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
