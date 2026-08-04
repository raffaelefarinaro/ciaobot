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

pub fn request(kind: PermissionKind) -> PermissionState {
    match kind {
        PermissionKind::Microphone => request_microphone_permission(),
        PermissionKind::Notifications => request_notification_permission(),
        PermissionKind::Camera => PermissionState::NotDetermined,
    }
}
