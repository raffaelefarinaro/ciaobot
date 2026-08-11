use crate::capture::TraySnapshot;
use std::collections::HashSet;
use tauri::{
    AppHandle,
    menu::{CheckMenuItemBuilder, Menu, MenuItem, MenuItemBuilder, PredefinedMenuItem, Submenu},
};

// The working indicator is an animated glyph in the row's own text.
//
// The legacy menu bar used a pulsing template *icon* here, which cannot be
// reproduced: muda renders menu-item images literally (it never calls
// setTemplate:), and an attached image did not draw at all — verified with
// working_rows=1 in the tray log and no error from either the builder or
// set_icon. Row text does draw, needs no template support, and inverts with the
// row highlight for free.
//
// The frames rotate rather than swell, and come from the Braille Patterns block
// on purpose: the menu font is proportional, so glyphs of differing advance
// width (◌ ◍ ◉ ●) shifted the whole label on every frame. Every braille pattern
// occupies the same fixed cell, so the text never moves.
pub const WORKING_PULSE_FRAMES: usize = 8;

pub fn working_pulse_glyph(index: usize) -> &'static str {
    ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧"][index % WORKING_PULSE_FRAMES]
}

/// A working chat's row, kept so the pulse can be re-rendered in place.
pub struct WorkingRow {
    pub item: MenuItem<tauri::Wry>,
    /// The label without the leading pulse glyph.
    pub label: String,
}

/// A built tray menu plus the working-chat rows whose pulse gets animated.
pub struct TrayMenu {
    pub menu: Menu<tauri::Wry>,
    pub working_items: Vec<WorkingRow>,
}

pub fn status_label(snapshot: &TraySnapshot) -> String {
    if !snapshot.reachable {
        return "Engine: not running".into();
    }
    let role = snapshot.startup.node_role.to_ascii_lowercase();
    if matches!(role.as_str(), "client" | "standby") {
        if !snapshot.startup.overall_ready {
            return "Client — waiting for host…".into();
        }
        if let Ok(peer) = url::Url::parse(&snapshot.startup.active_peer_url)
            && let Some(host) = peer.host_str()
        {
            return format!("Client — connected to {host}");
        }
        return "Engine: client (no host)".into();
    }
    if !snapshot.startup.overall_ready {
        return "Engine: starting…".into();
    }
    "Engine: host".into()
}

pub fn is_client(snapshot: &TraySnapshot) -> bool {
    let role = snapshot.startup.node_role.to_ascii_lowercase();
    matches!(role.as_str(), "client" | "standby")
}

// The engine reports availability against the GitHub release tag, which ships
// the engine and the app together, so one label covers both halves.
pub fn update_label(snapshot: &TraySnapshot) -> String {
    if snapshot.startup.update_available && !snapshot.startup.latest_version.is_empty() {
        return format!("Update to {}…", snapshot.startup.latest_version);
    }
    "Update".into()
}

pub fn chat_title(
    title: &str,
    workspace: Option<&str>,
    show_workspace: bool,
    unread: bool,
    needs_input: bool,
    working_marker: Option<&str>,
) -> String {
    let mut markers = String::new();
    if let Some(glyph) = working_marker {
        markers.push_str(glyph);
        markers.push(' ');
    }
    if needs_input {
        markers.push_str("! ");
    } else if unread {
        markers.push_str("● ");
    }
    let suffix = if show_workspace {
        workspace
            .filter(|value| !value.is_empty())
            .map(|value| format!(" — {value}"))
            .unwrap_or_default()
    } else {
        String::new()
    };
    format!("{markers}{title}{suffix}")
}

fn append_item(
    menu: &Menu<tauri::Wry>,
    app: &AppHandle,
    id: &str,
    text: &str,
) -> tauri::Result<()> {
    menu.append(&MenuItemBuilder::with_id(id, text).build(app)?)
}

fn append_separator(menu: &Menu<tauri::Wry>, app: &AppHandle) -> tauri::Result<()> {
    menu.append(&PredefinedMenuItem::separator(app)?)
}

pub fn build_menu(
    app: &AppHandle,
    snapshot: &TraySnapshot,
    notifications_enabled: bool,
    notifications_denied: bool,
    start_at_login: bool,
    hide_dock_icon: bool,
) -> tauri::Result<TrayMenu> {
    let menu = Menu::new(app)?;
    let mut working_items = Vec::new();
    append_item(&menu, app, "open", "Open Ciaobot")?;
    menu.append(
        &MenuItemBuilder::with_id("server-status", status_label(snapshot))
            .enabled(false)
            .build(app)?,
    )?;
    if is_client(snapshot) {
        append_item(&menu, app, "disconnect", "Disconnect from Host…")?;
    }
    if !snapshot.chats.is_empty() {
        append_separator(&menu, app)?;
        // A disabled header names the section, matching platform menu convention.
        menu.append(
            &MenuItemBuilder::with_id("chats-header", "Chats")
                .enabled(false)
                .build(app)?,
        )?;
        let workspaces = snapshot
            .chats
            .iter()
            .filter_map(|chat| chat.workspace.as_deref())
            .collect::<HashSet<_>>();
        let show_workspace = workspaces.len() > 1;
        let active = snapshot
            .active_chat_ids
            .iter()
            .map(String::as_str)
            .collect::<HashSet<_>>();
        for chat in &snapshot.chats {
            let working = active.contains(chat.chat_id.as_str());
            let id = format!("chat:{}", chat.chat_id);
            let label = chat_title(
                &chat.title,
                chat.workspace.as_deref(),
                show_workspace,
                chat.unread,
                chat.needs_input,
                None,
            );
            if working {
                let item = MenuItem::with_id(
                    app,
                    &id,
                    format!("{} {label}", working_pulse_glyph(0)),
                    true,
                    None::<&str>,
                )?;
                menu.append(&item)?;
                working_items.push(WorkingRow { item, label });
            } else {
                append_item(&menu, app, &id, &label)?;
            }
        }
    }

    append_separator(&menu, app)?;
    let advanced = Submenu::new(app, "Advanced", true)?;
    advanced.append(
        &MenuItemBuilder::with_id(
            "app-version",
            format!("App version {}", env!("CARGO_PKG_VERSION")),
        )
        .enabled(false)
        .build(app)?,
    )?;
    advanced.append(&PredefinedMenuItem::separator(app)?)?;
    advanced.append(&MenuItemBuilder::with_id("logs", "View Logs").build(app)?)?;

    advanced.append(
        &CheckMenuItemBuilder::with_id(
            "notifications",
            if notifications_denied {
                "Native Notifications (enable in System Settings)"
            } else {
                "Native Notifications"
            },
        )
        .checked(notifications_enabled)
        .build(app)?,
    )?;
    advanced.append(
        &CheckMenuItemBuilder::with_id("start-at-login", "Start at Login")
            .checked(start_at_login)
            .build(app)?,
    )?;
    advanced.append(
        &CheckMenuItemBuilder::with_id("hide-dock-icon", "Hide Dock Icon")
            .checked(hide_dock_icon)
            .build(app)?,
    )?;
    advanced.append(&PredefinedMenuItem::separator(app)?)?;
    advanced.append(&MenuItemBuilder::with_id("github", "View on GitHub").build(app)?)?;
    advanced.append(&MenuItemBuilder::with_id("report-issue", "Report an Issue").build(app)?)?;
    menu.append(&advanced)?;

    append_separator(&menu, app)?;
    append_item(&menu, app, "update", &update_label(snapshot))?;
    append_item(
        &menu,
        app,
        if snapshot.reachable {
            "restart"
        } else {
            "start"
        },
        if snapshot.reachable {
            "Restart"
        } else {
            "Start"
        },
    )?;
    append_item(&menu, app, "quit", "Quit")?;
    Ok(TrayMenu {
        menu,
        working_items,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capture::StartupStatus;

    #[test]
    fn status_labels_cover_host_client_starting_and_stopped() {
        assert_eq!(
            status_label(&TraySnapshot::default()),
            "Engine: not running"
        );
        let starting = TraySnapshot {
            reachable: true,
            ..TraySnapshot::default()
        };
        assert_eq!(status_label(&starting), "Engine: starting…");
        let waiting = TraySnapshot {
            reachable: true,
            startup: StartupStatus {
                node_role: "client".into(),
                ..StartupStatus::default()
            },
            ..TraySnapshot::default()
        };
        assert_eq!(status_label(&waiting), "Client — waiting for host…");
        let client = TraySnapshot {
            reachable: true,
            startup: StartupStatus {
                overall_ready: true,
                node_role: "client".into(),
                active_peer_url: "http://10.0.0.4:8443".into(),
                ..StartupStatus::default()
            },
            ..TraySnapshot::default()
        };
        assert_eq!(status_label(&client), "Client — connected to 10.0.0.4");
        let host = TraySnapshot {
            reachable: true,
            startup: StartupStatus {
                overall_ready: true,
                ..StartupStatus::default()
            },
            ..TraySnapshot::default()
        };
        assert_eq!(status_label(&host), "Engine: host");
    }

    #[test]
    fn disconnect_is_available_only_for_client_nodes() {
        let host = TraySnapshot {
            reachable: true,
            startup: StartupStatus {
                node_role: "host".into(),
                ..StartupStatus::default()
            },
            ..TraySnapshot::default()
        };
        let client = TraySnapshot {
            startup: StartupStatus {
                node_role: "client".into(),
                ..StartupStatus::default()
            },
            ..TraySnapshot::default()
        };
        let legacy_client = TraySnapshot {
            startup: StartupStatus {
                node_role: "standby".into(),
                ..StartupStatus::default()
            },
            ..TraySnapshot::default()
        };

        assert!(!is_client(&host));
        assert!(is_client(&client));
        assert!(is_client(&legacy_client));
    }

    #[test]
    fn chat_titles_preserve_attention_working_and_workspace_state() {
        // The pulse glyph leads, and the unread/needs-input markers survive
        // alongside it exactly as they did with the old static ◌.
        assert_eq!(
            chat_title("Inbox", Some("work"), true, true, false, Some("⠹")),
            "⠹ ● Inbox — work"
        );
        assert_eq!(
            chat_title("Approval", Some("work"), false, true, true, None),
            "! Approval"
        );
    }

    #[test]
    fn a_row_with_no_pulse_carries_no_working_marker() {
        // The animator re-renders working rows as "<glyph> <label>", so the
        // stored label must not already contain a glyph or it would double up.
        assert_eq!(
            chat_title("Inbox", Some("work"), true, true, false, None),
            "● Inbox — work"
        );
        assert_eq!(
            chat_title("Quiet", None, false, false, false, None),
            "Quiet"
        );
    }

    #[test]
    fn the_spinner_rotates_at_a_fixed_width_and_wraps() {
        let cycle: Vec<&str> = (0..WORKING_PULSE_FRAMES).map(working_pulse_glyph).collect();
        assert_eq!(cycle, ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧"]);

        // Every frame must be one char from the Braille Patterns block, or a
        // differing advance width would shift the label on each tick — which is
        // exactly what the ◌/◍/◉/● pulse did.
        for glyph in cycle {
            let mut chars = glyph.chars();
            let code = chars.next().expect("a frame is never empty") as u32;
            assert!(chars.next().is_none(), "{glyph} must be a single char");
            assert!(
                (0x2800..=0x28FF).contains(&code),
                "{glyph} is outside Braille Patterns"
            );
        }

        // The animator feeds an ever-increasing counter, so it has to wrap.
        assert_eq!(working_pulse_glyph(WORKING_PULSE_FRAMES), "⠋");
        assert_eq!(working_pulse_glyph(WORKING_PULSE_FRAMES + 2), "⠹");
    }

    #[test]
    fn update_label_names_the_available_version_when_there_is_one() {
        let mut snapshot = TraySnapshot {
            reachable: true,
            ..TraySnapshot::default()
        };
        // Nothing pending: a bare verb, no ellipsis, since the click does not
        // open a further choice.
        assert_eq!(update_label(&snapshot), "Update");

        snapshot.startup.update_available = true;
        snapshot.startup.latest_version = "0.6.1".into();
        assert_eq!(update_label(&snapshot), "Update to 0.6.1…");

        // A truthy flag with no version (engine could not reach GitHub) must
        // not render "Update to …".
        snapshot.startup.latest_version = String::new();
        assert_eq!(update_label(&snapshot), "Update");
    }
}
