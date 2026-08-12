use serde_json::Value;
use std::{
    collections::{HashMap, HashSet},
    fs,
    path::PathBuf,
};

/// Tracks which notification entries this process has already posted.
///
/// The cursor is a timestamp rather than a byte offset because the entries
/// normally arrive from the engine over HTTP, not from the local file. In
/// client mode `notifications.jsonl` on this machine is never written — the
/// host runs the chats — so a plain file tail stays silent forever and the
/// native banner, the only reliable channel, never fires on a client. The file
/// remains the fallback for when the engine is unreachable.
///
/// The cursor starts unset and the first poll only primes it, so launching the
/// app (or connecting to a host with a full log) never replays a backlog.
///
/// `primed` records that first poll separately from `cursor`. An empty log at
/// launch leaves the cursor unset, so deriving "is this the priming poll?" from
/// `cursor.is_none()` would prime twice and swallow the first real entry.
#[derive(Debug)]
pub struct NotificationLogTail {
    path: PathBuf,
    cursor: Option<f64>,
    primed: bool,
    seen: HashSet<String>,
}

fn entry_ts(entry: &Value) -> f64 {
    entry.get("ts").and_then(Value::as_f64).unwrap_or(0.0)
}

fn entry_id(entry: &Value) -> String {
    entry.to_string()
}

fn is_clear_entry(entry: &Value) -> bool {
    entry.get("kind").and_then(Value::as_str) == Some("clear")
}

fn entry_chat_id(entry: &Value) -> &str {
    entry.get("chat_id").and_then(Value::as_str).unwrap_or("")
}

impl NotificationLogTail {
    pub fn at_end(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            cursor: None,
            primed: false,
            seen: HashSet::new(),
        }
    }

    /// Timestamp to ask the engine for, inclusive of the newest entry seen.
    pub fn cursor(&self) -> f64 {
        self.cursor.unwrap_or(0.0)
    }

    /// Entries appended since the last poll, oldest first.
    ///
    /// `fetched` is the engine's answer, or `None` when the call failed — the
    /// caller owns the HTTP so this stays testable without a server.
    pub fn poll(&mut self, fetched: Option<Vec<Value>>) -> Vec<Value> {
        let priming = !self.primed;
        self.primed = true;
        let entries = match fetched {
            Some(entries) => entries,
            // Engine down: read this machine's own log, applying the cursor the
            // engine would have applied.
            None => self.read_file(),
        };

        let newest = entries
            .iter()
            .map(entry_ts)
            .fold(None::<f64>, |acc, ts| Some(acc.map_or(ts, |a| a.max(ts))));
        let fresh: Vec<Value> = entries
            .iter()
            .filter(|entry| !self.seen.contains(&entry_id(entry)))
            .cloned()
            .collect();

        if let Some(newest) = newest {
            // The cursor is inclusive, so entries sharing the newest timestamp
            // come back on the next poll too. Remembering only those IDs
            // dedupes them while keeping the set bounded.
            self.cursor = Some(newest);
            self.seen = entries
                .iter()
                .filter(|entry| entry_ts(entry) >= newest)
                .map(entry_id)
                .collect();
        }

        // Do not replay old banners at launch, but do replay clear controls:
        // a read may have happened while this companion was not running and
        // the delivered macOS banner can still be present in Notification
        // Center from the previous process.
        //
        // Only the *last* entry for a chat describes that chat's current state.
        // Replaying every clear in the log meant an old read dismissed a newer,
        // still-unread banner for the same chat at launch: read chat X (clear
        // logged), a new message for X arrives and posts a banner, then the app
        // restarts and re-applies the stale clear. A clear speaks for a chat
        // only when nothing followed it.
        if priming {
            let mut last_index: HashMap<&str, usize> = HashMap::new();
            for (index, entry) in entries.iter().enumerate() {
                last_index.insert(entry_chat_id(entry), index);
            }
            entries
                .iter()
                .enumerate()
                .filter(|(index, entry)| {
                    is_clear_entry(entry) && last_index.get(entry_chat_id(entry)) == Some(index)
                })
                .map(|(_, entry)| entry.clone())
                .collect()
        } else {
            fresh
        }
    }

    /// Local log entries at or after the cursor. A malformed or partially
    /// written line is skipped; the next poll sees it whole.
    fn read_file(&self) -> Vec<Value> {
        let Ok(text) = fs::read_to_string(&self.path) else {
            return Vec::new();
        };
        let since = self.cursor.unwrap_or(f64::NEG_INFINITY);
        text.lines()
            .filter_map(|line| serde_json::from_str::<Value>(line.trim()).ok())
            .filter(|entry| entry.is_object() && entry_ts(entry) >= since)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;
    use tempfile::tempdir;

    fn entry(ts: f64, title: &str) -> Value {
        json!({"ts": ts, "title": title})
    }

    #[test]
    fn the_first_poll_primes_the_cursor_without_replaying() {
        let mut tail = NotificationLogTail::at_end("/nonexistent");
        assert!(
            tail.poll(Some(vec![entry(10.0, "old")])).is_empty(),
            "launching must not replay the backlog"
        );
        assert_eq!(tail.cursor(), 10.0);

        let posted = tail.poll(Some(vec![entry(10.0, "old"), entry(11.0, "new")]));
        assert_eq!(posted.len(), 1);
        assert_eq!(posted[0]["title"], "new");
    }

    // A tray launched before any notification exists primes against an empty
    // feed, which leaves the cursor unset. Deriving "am I priming?" from the
    // cursor therefore primed twice and swallowed the first real entry.
    #[test]
    fn the_first_entry_after_an_empty_prime_is_posted() {
        let mut tail = NotificationLogTail::at_end("/nonexistent");
        assert!(tail.poll(Some(vec![])).is_empty(), "nothing to replay yet");

        let posted = tail.poll(Some(vec![entry(1.0, "first ever")]));
        assert_eq!(posted.len(), 1);
        assert_eq!(posted[0]["title"], "first ever");
        assert!(tail.poll(Some(vec![entry(1.0, "first ever")])).is_empty());
    }

    // The cursor is inclusive, so the boundary entry is re-sent every poll and
    // only the seen-ID set stops it from being posted twice.
    #[test]
    fn an_entry_on_the_cursor_is_posted_once() {
        let mut tail = NotificationLogTail::at_end("/nonexistent");
        tail.poll(Some(vec![entry(10.0, "old")]));
        assert_eq!(tail.poll(Some(vec![entry(11.0, "new")])).len(), 1);
        assert!(tail.poll(Some(vec![entry(11.0, "new")])).is_empty());
    }

    // Two notifications can share a timestamp; an exclusive cursor would drop
    // one of them silently.
    #[test]
    fn entries_sharing_the_newest_timestamp_all_arrive() {
        let mut tail = NotificationLogTail::at_end("/nonexistent");
        tail.poll(Some(vec![entry(10.0, "old")]));
        let posted = tail.poll(Some(vec![entry(11.0, "a"), entry(11.0, "b")]));
        assert_eq!(posted.len(), 2);
        assert!(
            tail.poll(Some(vec![entry(11.0, "a"), entry(11.0, "b")]))
                .is_empty()
        );
    }

    #[test]
    fn falls_back_to_the_local_log_when_the_engine_is_unreachable() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("notifications.jsonl");
        fs::write(&path, "{\"ts\":1.0,\"title\":\"old\"}\n").unwrap();
        let mut tail = NotificationLogTail::at_end(&path);
        assert!(tail.poll(None).is_empty());

        fs::write(
            &path,
            "{\"ts\":1.0,\"title\":\"old\"}\nbad\n{\"ts\":2.0,\"title\":\"new\"}\n",
        )
        .unwrap();
        let posted = tail.poll(None);
        assert_eq!(posted.len(), 1);
        assert_eq!(posted[0]["title"], "new");
        assert!(tail.poll(None).is_empty());
    }

    #[test]
    fn a_missing_log_is_not_an_error() {
        let mut tail = NotificationLogTail::at_end("/nonexistent/notifications.jsonl");
        assert!(tail.poll(None).is_empty());
        assert!(tail.poll(None).is_empty());
    }

    #[test]
    fn the_first_poll_preserves_clear_controls_without_replaying_banners() {
        let mut tail = NotificationLogTail::at_end("/nonexistent");
        let entries = vec![
            entry(1.0, "old banner"),
            json!({"ts": 2.0, "kind": "clear", "chat_id": "chat-1"}),
        ];
        let pending = tail.poll(Some(entries));
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0]["kind"], "clear");
    }

    // A clear only describes a chat that has not spoken since. Replaying every
    // clear in the log dismissed a banner the user had never seen: read chat-1,
    // a new message for chat-1 arrives, then the app restarts.
    #[test]
    fn a_clear_superseded_by_a_newer_message_is_not_replayed() {
        let mut tail = NotificationLogTail::at_end("/nonexistent");
        let entries = vec![
            json!({"ts": 1.0, "kind": "clear", "chat_id": "chat-1"}),
            json!({"ts": 2.0, "title": "unread again", "chat_id": "chat-1"}),
            json!({"ts": 3.0, "kind": "clear", "chat_id": "chat-2"}),
        ];
        let pending = tail.poll(Some(entries));
        assert_eq!(
            pending.len(),
            1,
            "only the clear that nothing followed may replay"
        );
        assert_eq!(pending[0]["chat_id"], "chat-2");
    }
}
