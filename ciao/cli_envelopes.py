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


def task_notification_fields(content: str) -> dict[str, str] | None:
    """Fields of the first ``<task-notification>`` anywhere in `content`.

    Does not ask whether `content` *is* a notification — use
    :func:`envelope_notification_fields` for that. This one exists for a
    caller that already knows what it is holding.
    """
    m = TASK_NOTIFICATION_RE.search(content)
    if not m:
        return None
    return {tag: text.strip() for tag, text in INNER_TAG_RE.findall(m.group(1))}


def envelope_notification_fields(content: str) -> dict[str, str] | None:
    """Fields of the notification `content` **is**, else None.

    A completion is a record the CLI wrote as a ``<task-notification>``
    envelope, so it has to *open* with that tag. Text that merely carries the
    grammar in its body is something else that happens to quote it, and both
    readers get that wrong in the same expensive way if they only search:

    * ``<bash-stdout>`` from a command that printed a session JSONL (``cat``,
      ``grep task-notification``) flips a running agent to "failed" out of
      shell output, and opens a synthesis-nudge window for a completion that
      never happened;
    * a human message quoting a notification ("why did this fail? …") does the
      same, and is skipped by the turn counter while the renderer shows it as
      a user bubble — which is exactly the ``turn_index`` drift this module
      exists to prevent.

    The body search stays unanchored *within* such a record: a notification
    with anything appended after the closing tag is still a notification, and
    only the first is read when the CLI concatenated several.
    """
    if opening_envelope_tag(content) != "task-notification":
        return None
    return task_notification_fields(content)
