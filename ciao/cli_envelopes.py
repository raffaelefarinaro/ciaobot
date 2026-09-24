"""Shared vocabulary for the CLI's synthetic user records.

The Claude Code CLI writes user-role records into the session JSONL that the
human never typed: subagent completion notifications, bash output, echoes of
slash commands, interrupt sentinels. Two subsystems have to agree on exactly
which records those are:

- ``ciao/subagent_tracking.py`` parses the parent JSONL and must not let a
  synthetic record advance ``turn_index``;
- ``ciao/web/transcript_service.py`` renders the same JSONL and must not let a
  synthetic record render as a user bubble.

The two counters are anchored to each other — a record skipped by one and kept
by the other shifts every subsequent ``turn_index`` — so the predicates live
here once instead of as near-copies in both modules. They drifted while they
were copies (see issue #500), which is the reason this module exists.
"""

from __future__ import annotations

import re

# CLI-internal user-message envelopes. The Claude Code CLI synthesizes
# user-role messages wrapped in these XML tags to feed the parent agent
# subagent completion, bash output, slash-command invocations, etc. They are
# NOT from the human; they're the CLI talking to its own model. The tag names
# come from the constant table in claude_agent_sdk/_bundled/claude
# (IO="task-notification", EtH="bash-input", WV="command-name", and so on).
#
# Without this filter the envelopes leak into chat history as user bubbles:
# the browser strips the unknown tags and lays out only the inner text,
# producing the "task_id  toolu_id  /tmp/.../output completed\nAgent ..."
# blocks visible in chats with parallel subagents.
CLI_ENVELOPE_TAGS = (
    "task-notification",
    "bash-input",
    "bash-stdout",
    "bash-stderr",
    "bash-exit-code",
    "local-command-stdout",
    "local-command-stderr",
    "local-command-caveat",
    "command-name",
    "command-message",
    "command-args",
    "remote-review",
    "remote-review-progress",
    "teammate-message",
    "cross-session-message",
    "fork-boilerplate",
)

# Start-anchored: a record counts as an envelope when it *opens* with one of
# the tags. Text that merely mentions a tag further in is the human's own
# prose and still renders as their bubble.
# The tag is captured so callers can tell *which* envelope opened the record
# (see ``opening_envelope_tag``): a record that merely mentions another
# envelope's grammar inside its body is still that outer envelope.
CLI_ENVELOPE_RE = re.compile(
    r"^\s*<(" + "|".join(re.escape(t) for t in CLI_ENVELOPE_TAGS) + r")(?:\s[^>]*)?>"
)

# Unanchored and non-greedy, used with ``search``: a notification is
# recognised wherever it sits in the record, and only the first one is
# captured when several are concatenated into a single record. Anchoring it to
# the whole record would miss a notification with anything appended after the
# closing tag; a greedy body would swallow the gap between two notifications
# and mix their fields together.
TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>(.*?)</task-notification>", re.DOTALL
)

# Pulls <tag>content</tag> pairs out of a task-notification body. Names match
# the schema fields the CLI emits (task-id, tool-use-id, output-file, status,
# summary, plus an optional task-type).
INNER_TAG_RE = re.compile(r"<([a-z-]+)>(.*?)</\1>", re.DOTALL)

# Slash commands the Claude Agent SDK injects as user turns when the PWA
# changes model or mode mid-session (via ClaudeSDKClient.set_model /
# set_permission_mode). They end up in the session JSONL and would otherwise
# render as user bubbles the user didn't type. The assistant acknowledgement
# ("Set model to ..." / "Set mode to ...") is collapsed into a single system
# bubble by the transcript renderer.
CONTROL_SLASH_PREFIXES = ("/model", "/mode")

# Sentinel that the Claude Code CLI writes into the session JSONL when a turn
# is interrupted (steer/queue mid-stream) or hits an empty rate-limit error.
# It's the `UXH` constant in claude_agent_sdk/_bundled/claude. Claude Code's
# own UI hides these (`case UXH: return null`); we mirror that so reloads
# don't render a literal "No response requested." bubble after every interrupt.
NO_RESPONSE_SENTINEL = "No response requested."

COMPACT_SUMMARY_PREFIX = "This session is being continued from a previous conversation"

_CONTEXT_BLOCK_RE = re.compile(
    r"^\[CIAO_CONTEXT_BEGIN\]\n.*?\n\[CIAO_CONTEXT_END\]\n\n",
    re.DOTALL,
)

_TASK_ID_SENTINEL_PREFIX = "__orphan_summary"

# Matches the Claude Agent SDK's own _SKIP_FIRST_PROMPT_PATTERN
# ([Request interrupted by user[^\]]*]) so we cover every CLI variant, not
# just the bare form. Steer/queue interrupts of an in-flight tool call produce
# "[Request interrupted by user for tool use]" — without this wildcard that
# variant survives as a synthetic user record and renders as a quoted bubble
# that looks like an error reply to a question.
INTERRUPTED_REQUEST_RE = re.compile(r"\[Request interrupted by user[^\]]*\]")


def is_cli_envelope(content: str) -> bool:
    """True when `content` opens with a CLI-synthesized user-message wrapper."""
    return bool(CLI_ENVELOPE_RE.match(content))


def opening_envelope_tag(content: str) -> str | None:
    """The envelope tag `content` opens with, or None when it opens with none.

    Tells a ``<task-notification>`` record apart from another envelope that
    merely carries that grammar in its body — ``<bash-stdout>`` of a command
    that printed a session JSONL, for one. Without the distinction such a
    record renders as a fabricated "Subagent failed: ..." status line built
    out of shell output.
    """
    m = CLI_ENVELOPE_RE.match(content)
    return m.group(1) if m else None


def is_control_slash_command(content: str) -> bool:
    """True when `content` is an SDK-injected /model or /mode turn."""
    text = content.strip()
    if not text:
        return False
    return text.split(None, 1)[0] in CONTROL_SLASH_PREFIXES


def is_no_response_sentinel(content: str) -> bool:
    """True when `content` is the CLI's interrupted-turn sentinel."""
    return content.strip() == NO_RESPONSE_SENTINEL


def is_interrupted_request_sentinel(content: str) -> bool:
    """True when `content` is nothing but an interrupt marker."""
    return bool(INTERRUPTED_REQUEST_RE.fullmatch(content.strip()))


def strip_injected_context(content: str) -> str:
    """Remove Ciaobot's leading context capsule from a CLI user record."""
    stripped = content
    while True:
        nxt = _CONTEXT_BLOCK_RE.sub("", stripped, count=1)
        if nxt == stripped:
            break
        stripped = nxt
    return stripped or content


def _flag_on(value: object, name: str) -> bool:
    if isinstance(value, dict):
        return bool(value.get(name))
    return bool(getattr(value, name, False))


def is_compact_summary(
    record: object = None, content: str = "", *, flagged: bool = False
) -> bool:
    """True when ``record`` is a CLI post-compaction recap.

    ``isCompactSummary`` is authoritative when present. The prefix remains a
    fallback for records written before the CLI stamped them. The record may
    be a raw JSONL dictionary or an SDK message object.
    """
    if isinstance(record, str) and not content:
        content = record
        record = None
    if flagged or _flag_on(record, "isCompactSummary"):
        return True
    inner = (
        record.get("message")
        if isinstance(record, dict)
        else getattr(record, "message", None)
    )
    if _flag_on(inner, "isCompactSummary"):
        return True
    return content.lstrip().startswith(COMPACT_SUMMARY_PREFIX)


def _leading_task_notification_matches(content: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    previous_end: int | None = None
    for match in TASK_NOTIFICATION_RE.finditer(content):
        if previous_end is None:
            if content[: match.start()].strip():
                break
        elif content[previous_end : match.start()].strip():
            break
        matches.append(match)
        previous_end = match.end()
    return matches


def _fields_from_task_notification(body: str) -> dict[str, str]:
    return {tag: text.strip() for tag, text in INNER_TAG_RE.findall(body)}


def _real_task_ids(body: str) -> list[str]:
    return [
        text.strip()
        for tag, text in INNER_TAG_RE.findall(body)
        if tag == "task-id"
        and text.strip()
        and not text.strip().startswith(_TASK_ID_SENTINEL_PREFIX)
    ]


def task_notification_fields(content: str) -> dict[str, str] | None:
    """Fields of the first ``<task-notification>`` anywhere in ``content``."""
    match = TASK_NOTIFICATION_RE.search(content)
    return _fields_from_task_notification(match.group(1)) if match else None


def envelope_notifications(content: str) -> list[tuple[dict[str, str], list[str]]]:
    """Return the leading notifications and their real task ids.

    A record may concatenate notifications or repeat ``task-id`` tags for a
    sweep. The scan stops at the first non-whitespace text after a match so a
    later shell-output quote cannot be mistaken for another completion.
    """
    if opening_envelope_tag(content) != "task-notification":
        return []
    return [
        (
            _fields_from_task_notification(match.group(1)),
            _real_task_ids(match.group(1)),
        )
        for match in _leading_task_notification_matches(content)
    ]


def _task_statuses_from_body(body: str, default_status: str) -> list[tuple[str, str]]:
    pending: list[str] = []
    current = default_status or "completed"
    statuses: list[tuple[str, str]] = []
    for tag, text in INNER_TAG_RE.findall(body):
        value = text.strip()
        if tag == "task-id":
            if value and not value.startswith(_TASK_ID_SENTINEL_PREFIX):
                pending.append(value)
        elif tag == "status":
            current = value or default_status or "completed"
            statuses.extend((task_id, current) for task_id in pending)
            pending.clear()
    statuses.extend((task_id, current) for task_id in pending)
    return statuses


def envelope_notification_task_statuses(content: str) -> list[tuple[str, str]]:
    """Return real task ids and their statuses from the leading notifications."""
    if opening_envelope_tag(content) != "task-notification":
        return []
    out: list[tuple[str, str]] = []
    for match in _leading_task_notification_matches(content):
        body = match.group(1)
        fields = _fields_from_task_notification(body)
        default_status = fields.get("status", "") or "completed"
        out.extend(_task_statuses_from_body(body, default_status))
    return out


def notification_task_ids(content: str) -> list[str]:
    """Return every real task id in the leading notification run."""
    return [
        task_id
        for _fields, task_ids in envelope_notifications(content)
        for task_id in task_ids
    ]


def envelope_notification_fields(content: str) -> dict[str, str] | None:
    """Fields of the first leading notification ``content`` is, else None."""
    notifications = envelope_notifications(content)
    return notifications[0][0] if notifications else None
