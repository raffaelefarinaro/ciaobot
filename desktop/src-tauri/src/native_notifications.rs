use mac_usernotifications::{Notification, close_delivered, get_delivered_notification_ids};
use objc2::{AnyThread, define_class, rc::Retained};
use objc2_foundation::{NSObject, NSObjectProtocol};
use objc2_user_notifications::{
    UNNotification, UNNotificationPresentationOptions, UNNotificationResponse,
    UNUserNotificationCenter, UNUserNotificationCenterDelegate,
};
use serde_json::Value;
use std::path::Path;
use std::sync::{Mutex, OnceLock};
use uuid::Uuid;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NavigationIntent {
    Chat(String),
    Workspaces,
}

impl NavigationIntent {
    fn notification_id(&self) -> String {
        match self {
            Self::Chat(chat_id) => format!("ciaobot:chat:{chat_id}:{}", Uuid::new_v4()),
            Self::Workspaces => format!("ciaobot:workspaces:{}", Uuid::new_v4()),
        }
    }

    fn from_notification_id(identifier: &str) -> Option<Self> {
        if let Some(value) = identifier.strip_prefix("ciaobot:chat:") {
            let (chat_id, _) = value.rsplit_once(':')?;
            return (!chat_id.is_empty()).then(|| Self::Chat(chat_id.to_string()));
        }
        identifier
            .starts_with("ciaobot:workspaces:")
            .then_some(Self::Workspaces)
    }
}

type ActionHandler = Box<dyn Fn(NavigationIntent) + Send + Sync + 'static>;
static ACTION_HANDLER: OnceLock<Mutex<Option<ActionHandler>>> = OnceLock::new();

fn action_handler() -> &'static Mutex<Option<ActionHandler>> {
    ACTION_HANDLER.get_or_init(|| Mutex::new(None))
}

define_class!(
    #[unsafe(super(NSObject))]
    #[name = "CiaobotNotificationDelegate"]
    struct CiaobotNotificationDelegate;

    unsafe impl NSObjectProtocol for CiaobotNotificationDelegate {}

    unsafe impl UNUserNotificationCenterDelegate for CiaobotNotificationDelegate {
        #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
        fn will_present_notification(
            &self,
            _center: &UNUserNotificationCenter,
            _notification: &UNNotification,
            completion_handler: &block2::DynBlock<dyn Fn(UNNotificationPresentationOptions)>,
        ) {
            completion_handler.call((UNNotificationPresentationOptions::Banner
                | UNNotificationPresentationOptions::Sound,));
        }

        #[unsafe(method(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:))]
        fn did_receive_response(
            &self,
            _center: &UNUserNotificationCenter,
            response: &UNNotificationResponse,
            completion_handler: &block2::DynBlock<dyn Fn()>,
        ) {
            let action = response.actionIdentifier().to_string();
            if action != "com.apple.UNNotificationDismissActionIdentifier" {
                let identifier = response.notification().request().identifier().to_string();
                if let Some(intent) = NavigationIntent::from_notification_id(&identifier)
                    && let Ok(handler) = action_handler().lock()
                    && let Some(handler) = handler.as_ref()
                {
                    handler(intent);
                }
            }
            completion_handler.call(());
        }
    }
);

impl CiaobotNotificationDelegate {
    fn new() -> Retained<Self> {
        let this = Self::alloc().set_ivars(());
        unsafe { objc2::msg_send![super(this), init] }
    }
}

static DELEGATE: OnceLock<Retained<CiaobotNotificationDelegate>> = OnceLock::new();

/// True when this process runs from inside a real `.app` bundle.
///
/// `UNUserNotificationCenter` requires one. In an unbundled process — `cargo
/// run`, and therefore `tauri dev` — `bundleProxyForCurrentProcess` is nil and
/// `currentNotificationCenter()` raises an `NSException` that nothing catches,
/// killing the process during `applicationDidFinishLaunching` before a window
/// ever appears. Every entry point into the notification center checks this
/// first, so an unbundled run gets inert notifications instead of a crash.
fn is_bundled() -> bool {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(Path::to_path_buf))
        .is_some_and(|dir| dir.ends_with("Contents/MacOS"))
}

fn set_action_delegate() {
    if !is_bundled() {
        return;
    }
    let delegate = DELEGATE.get_or_init(CiaobotNotificationDelegate::new);
    UNUserNotificationCenter::currentNotificationCenter()
        .setDelegate(Some(objc2::runtime::ProtocolObject::from_ref(&**delegate)));
}

/// Register before the app event loop starts so macOS can deliver an action
/// that launched this process from an existing Notification Center item.
pub fn install_action_listener(handler: impl Fn(NavigationIntent) + Send + Sync + 'static) {
    if let Ok(mut current) = action_handler().lock() {
        *current = Some(Box::new(handler));
    }
    set_action_delegate();
}

fn restore_action_listener() {
    set_action_delegate();
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NativeNotification {
    pub title: String,
    pub body: String,
    pub thread_id: Option<String>,
    pub intent: Option<NavigationIntent>,
}

impl NativeNotification {
    pub fn from_value(payload: &Value) -> Self {
        let title = payload
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or("Ciaobot")
            .to_string();
        let body = payload
            .get("body")
            .or_else(|| payload.get("message"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let chat_id = payload
            .get("chat_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_string);
        let kind = payload
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_ascii_lowercase();
        let profile = payload
            .get("profile")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty());
        let intent = chat_id
            .as_ref()
            .map(|value| NavigationIntent::Chat(value.clone()))
            .or_else(|| {
                (profile.is_some() || kind.contains("gws")).then_some(NavigationIntent::Workspaces)
            });
        Self {
            title,
            body,
            thread_id: chat_id,
            intent,
        }
    }

    pub async fn post(self) -> Result<(), String> {
        // `send()` reaches the same notification center as the delegate, so an
        // unbundled run has to bail here too rather than crash on delivery.
        if !is_bundled() {
            return Err("native notifications require a bundled .app".to_string());
        }
        let identifier = self
            .intent
            .as_ref()
            .map(NavigationIntent::notification_id)
            .unwrap_or_else(|| format!("ciaobot:main:{}", Uuid::new_v4()));
        let mut notification = Notification::new()
            .title(&self.title)
            .message(&self.body)
            .default_sound()
            .id(&identifier);
        if let Some(thread_id) = self.thread_id {
            notification = notification.thread_id(thread_id);
        }
        notification
            .send()
            .await
            .map_err(|error| error.to_string())?;
        // mac-usernotifications installs its own process-local response
        // delegate while sending. Restore the app-level listener; the intent
        // lives in the notification identifier so it also survives relaunch.
        restore_action_listener();
        Ok(())
    }
}

fn is_chat_notification(identifier: &str, chat_id: &str) -> bool {
    matches!(
        NavigationIntent::from_notification_id(identifier),
        Some(NavigationIntent::Chat(candidate)) if candidate == chat_id
    )
}

/// Remove delivered Ciaobot banners belonging to a chat.
pub async fn dismiss_chat_notifications(chat_id: &str) {
    if !is_bundled() || chat_id.is_empty() {
        return;
    }
    let identifiers = get_delivered_notification_ids().await;
    for identifier in identifiers {
        if is_chat_notification(&identifier, chat_id) {
            close_delivered(&identifier).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn unbundled_test_binary_is_not_treated_as_bundled() {
        // The test harness runs from target/debug/deps, not Contents/MacOS, so
        // this is the same path `tauri dev` takes. If this ever returns true
        // the notification-center guard is not protecting unbundled runs.
        assert!(!is_bundled());
    }

    #[test]
    fn maps_chat_and_gws_payloads_to_navigation_intents() {
        let chat = NativeNotification::from_value(&json!({
            "title": "Reply ready",
            "body": "Done",
            "kind": "result",
            "chat_id": "chat-1"
        }));
        assert_eq!(chat.intent, Some(NavigationIntent::Chat("chat-1".into())));
        assert_eq!(chat.thread_id.as_deref(), Some("chat-1"));

        let gws = NativeNotification::from_value(&json!({
            "message": "Reconnect Google",
            "kind": "gws-token-health",
            "profile": "work"
        }));
        assert_eq!(gws.title, "Ciaobot");
        assert_eq!(gws.body, "Reconnect Google");
        assert_eq!(gws.intent, Some(NavigationIntent::Workspaces));
    }

    #[test]
    fn malformed_payload_is_safe_and_has_no_navigation() {
        let notification = NativeNotification::from_value(&json!({"title": 12}));
        assert_eq!(notification.title, "Ciaobot");
        assert!(notification.body.is_empty());
        assert_eq!(notification.intent, None);
    }

    #[test]
    fn notification_identifier_round_trips_cold_start_intents() {
        for intent in [
            NavigationIntent::Chat("chat:with:colons".into()),
            NavigationIntent::Workspaces,
        ] {
            let identifier = intent.notification_id();
            assert_eq!(
                NavigationIntent::from_notification_id(&identifier),
                Some(intent)
            );
        }
        assert_eq!(NavigationIntent::from_notification_id("unrelated"), None);
    }

    #[test]
    fn identifies_delivered_chat_notifications_for_dismissal() {
        let identifier = NavigationIntent::Chat("chat-1".into()).notification_id();
        assert!(is_chat_notification(&identifier, "chat-1"));
        assert!(!is_chat_notification(&identifier, "chat-2"));
        assert!(!is_chat_notification("ciaobot:workspaces:abc", "chat-1"));
    }
}
