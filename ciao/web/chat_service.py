"""Chat-workflow domain logic behind ``ProjectChatManager``.

``ciao/web/project_chats.py`` owns the chat hierarchy itself: the state file,
the live streams, the background tasks, and every decision that needs the
manager's own state. The pure rules it applies on the way live here - how a
provider error is classified as retryable, how a fork or provider handover is
trimmed to a bounded transcript, how a chat title is derived or rejected, how a project doc's
frontmatter description is rewritten, and how a scheduled run is graded. Each
is a function of its arguments alone, so it can be exercised without building a
manager, and ``project_chats`` imports this as a module rather than by name so
a test that patches one helper has the manager see the patch.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ciao import provider_registry
from ciao.model_tiers import canonical_tier, is_tier
from ciao.schedules import supports_auto_archive

# Project-files surface (list + upload). Mirrors the union of the read-only
# workspace-file/image allowlists plus the new binary one (PDF, ZIP, office
# docs). Kept in sync intentionally: anything we let users upload, we also
# need to be able to serve back via one of the workspace endpoints.
_PROJECT_TEXT_EXTS = frozenset({
    ".md", ".markdown", ".txt",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".vue",
    ".css", ".html", ".json",
    ".yaml", ".yml", ".toml",
    ".sh", ".rs", ".go", ".java", ".xml", ".sql",
    ".cfg", ".ini", ".log", ".csv",
})

_PROJECT_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".ico",
})

_PROJECT_BINARY_EXTS = frozenset({
    ".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".mht", ".mhtml",
})

_PROJECT_UPLOAD_EXTS = _PROJECT_TEXT_EXTS | _PROJECT_IMAGE_EXTS | _PROJECT_BINARY_EXTS

_PROJECT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

_HANDOVER_ROLES = {"user", "assistant", "system"}

_FORK_MAX_MESSAGES = 80

_FORK_MAX_CHARS = 60_000

_PROVIDER_HANDOVER_MAX_MESSAGES = 12

_PROVIDER_HANDOVER_MAX_CHARS = 12_000


def _classify_file(path: Path) -> str:
    """Map a file path to one of: ``markdown | image | text | binary``.

    Anything outside the three allowlists falls back to ``binary`` so the UI
    can show it greyed-out with a download fallback. The file may not be
    representable by any of our viewers, but we still list it.
    """
    ext = path.suffix.lower()
    if ext in {".md", ".markdown"}:
        return "markdown"
    if ext in _PROJECT_IMAGE_EXTS:
        return "image"
    if ext in _PROJECT_TEXT_EXTS:
        return "text"
    return "binary"


# A vault_folder must be a single directory name under projects/active/ or
# projects/completed/. Reject path separators, parent-directory traversal,
# leading dots, and non-printable characters. Names are free-form (lowercase
# kebab-case is preferred but not enforced); see README "Project naming
# convention".
_VAULT_FOLDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _restored_postprocess(raw: object) -> dict:
    """Sanitize a persisted post-archive record on load.

    The pipeline is an in-process ``asyncio`` task, so a record still marked
    "running" is a record whose task died with the previous process. Downgrading
    it to "done" keeps the chat reporting the steps that did land instead of
    showing an activity indicator nothing is left alive to clear."""
    if not isinstance(raw, dict) or not raw:
        return {}
    state = dict(raw)
    if state.get("state") == "running":
        state["state"] = "done"
        state["step"] = ""
        state["interrupted"] = True
    return state


def _project_reference_key(value: str) -> str:
    """Normalize a display name or vault-folder slug for context matching."""

    return re.sub(r"[\W_]+", "-", str(value).casefold()).strip("-")


def _stable_vault_project_id(workspace: str, vault_folder: str) -> str:
    """Return the convergent id for a newly discovered vault-backed project."""

    identity = f"{workspace.casefold()}\0{vault_folder.casefold()}".encode("utf-8")
    return f"proj-{hashlib.sha256(identity).hexdigest()[:12]}"


def _iso_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _provider_label(provider: str) -> str:
    if not provider:
        return "Provider"
    return provider_registry.label(provider, short=True)


def _clean_handover_messages(messages: list[dict] | None) -> list[dict]:
    """Sanitize visible chat rows without applying history limits."""
    rows: list[dict] = []
    for raw in messages or []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role", "")).strip().lower()
        if role not in _HANDOVER_ROLES:
            continue
        content = str(raw.get("content", "")).strip()
        if not content:
            continue
        entry: dict = {
            "role": role,
            "content": content,
        }
        timestamp = str(raw.get("timestamp", "") or raw.get("sent_at", "")).strip()
        if timestamp:
            entry["timestamp"] = timestamp
        tool_name = str(raw.get("tool_name", "")).strip()
        if tool_name:
            entry["tool_name"] = tool_name
        if bool(raw.get("is_error")):
            entry["is_error"] = True
        images = raw.get("images")
        if isinstance(images, list):
            refs = [str(ref) for ref in images if str(ref)]
            if refs:
                entry["images"] = refs
        file_path = str(raw.get("file_path", "")).strip()
        if file_path:
            entry["file_path"] = file_path
        action = str(raw.get("action", "")).strip()
        if action:
            entry["action"] = action
        tool = str(raw.get("tool", "")).strip()
        if tool:
            entry["tool"] = tool
        rows.append(entry)
    return rows


def _normalize_handover_messages(
    messages: list[dict] | None,
    *,
    max_messages: int = _FORK_MAX_MESSAGES,
    max_chars: int = _FORK_MAX_CHARS,
) -> list[dict]:
    """Sanitize and bound visible rows for a fork or provider handover."""
    rows = _clean_handover_messages(messages)
    total_chars = sum(len(str(row.get("content", ""))) for row in rows)
    while (
        len(rows) > max_messages
        or total_chars > max_chars
    ) and rows:
        removed = rows.pop(0)
        total_chars -= len(str(removed.get("content", "")))
    return rows


def _handover_marker(
    *,
    old_provider: str,
    old_model: str,
    new_provider: str,
    new_model: str,
) -> dict:
    return {
        "role": "system",
        "content": (
            "Handed over from "
            f"{_provider_label(old_provider)} / {old_model} to "
            f"{_provider_label(new_provider)} / {new_model}."
        ),
        "timestamp": _now_iso(),
    }


def _is_retryable_quota_error(text: str) -> bool:
    low = (text or "").lower()
    # Claude Code uses "You've hit your session limit" in its user-facing
    # exhaustion banner, while the API-shaped error says "reached your
    # session usage limit". Both should arm the deferred hourly retry.
    if (
        "reached your session usage limit" in low
        or "hit your session limit" in low
    ):
        return True
    # Temporary model saturation is a capacity error rather than a 429/quota
    # error. Treat it as hourly retryable so the user does not have to keep the
    # chat open and press Retry manually.
    if "at capacity" in low:
        return True
    if any(needle in low for needle in ("out of credit", "out of credits", "spend limit", "insufficient credit", "credit balance")):
        return True
    # A provider that just states the limit, with no 429 and none of the vendor
    # phrasings above — opencode/OpenAI surfaces "The usage limit has been
    # reached". Pairing a limit noun with an exhaustion verb is unambiguous in a
    # way the bare nouns are not, which is why those still need the 429 marker
    # below: "quota" or "session" alone appears in plenty of prose that is not
    # an exhaustion error.
    if any(noun in low for noun in ("usage limit", "rate limit", "quota", "token limit")) and any(
        verb in low for verb in ("reached", "exceeded", "exhausted")
    ):
        return True
    if "429" not in low and "too many requests" not in low:
        return False
    return any(needle in low for needle in ("usage limit", "rate limit", "quota", "session"))


def _is_retryable_connection_error(text: str) -> bool:
    low = (text or "").lower()
    connection_indicators = (
        "enotfound",
        "econnrefused",
        "econnreset",
        "etimedout",
        "unable to connect",
        "failed to fetch",
        "network request failed",
        "temporary failure in name resolution",
        "dns resolution failed",
        "socket timeout",
        "gateway timeout",
        "bad gateway",
        "service unavailable",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway timeout",
        "connect timeout",
        "connection timeout",
        "connection timed out",
        # Upstream API dropped the streaming connection mid-response. The CLI
        # surfaces this as a banner; the Claude provider re-flags it as an
        # error. Kept in sync with ``_CONNECTION_DROP_MARKERS`` in
        # ``ciao/providers/claude.py``.
        "connection closed mid-response",
        "response above may be incomplete",
    )
    return any(indicator in low for indicator in connection_indicators)


def _is_retryable_provider_startup_error(text: str) -> bool:
    """Recognize a transient provider-launch failure before turn progress."""
    low = (text or "").lower()
    if "opencode serve exited" in low and (
        "database is locked" in low or "database is busy" in low
    ):
        return True
    # A server that stays alive but never answers /global/health is the same
    # transient startup wedge (shared SQLite contention with other opencode
    # processes); _ensure_server already retries it internally, so a chat
    # turn that still lands here should get the same bounded auto-retry as
    # the database-locked exit instead of failing outright.
    return "opencode serve did not become healthy" in low


def _is_retryable_auth_error(text: str) -> bool:
    """Recognize a transient OAuth session expiry that can recover on retry.

    The Claude CLI surfaces ``Failed to authenticate: OAuth session expired
    and could not be refreshed`` when its in-memory credentials lapsed
    mid-turn. The credentials are refreshed on the next process spawn, so
    retrying the turn (fast 30s interval, bounded like connection errors)
    recovers without user intervention. Keep this narrow: only the
    ``oauth session expired`` / ``could not be refreshed`` shape is
    retried, not every ``Failed to authenticate`` (e.g. revoked keys).

    ``Not logged in · Please run /login`` is the same class: the CLI reports
    it when the credentials it holds lapsed mid-turn, and the next spawn
    re-reads them from disk. It is retried on the same bounded ladder, so a
    genuinely signed-out install stops after ``_MAX_CONNECTION_DROP_RETRIES``
    instead of looping.
    """
    low = (text or "").lower()
    if "oauth session expired" in low:
        return True
    if "failed to authenticate" in low and "could not be refreshed" in low:
        return True
    if "session expired" in low and "could not be refreshed" in low:
        return True
    if "not logged in" in low and "/login" in low:
        return True
    return False


def _uuid8() -> str:
    return uuid.uuid4().hex[:8]


_PLACEHOLDER_TITLE_RE = re.compile(r"^New session\b", re.IGNORECASE)


def _normalize_tier(model: str) -> str:
    """Canonicalize a tier alias; a concrete model id passes through unchanged."""
    return canonical_tier(model) if is_tier(model) else model


_INJECTED_CONTEXT_MARKER = "[CIAO_CONTEXT_BEGIN]"


def _real_title(title: str) -> str | None:
    """Return *title* if it is a real provider title, else None.

    Providers seed a session with a placeholder default (opencode uses
    ``New session - <timestamp>``) and only later write the generated title.
    Treating the placeholder as a real title would let the auto-title poll
    stop early and leave the sidebar stuck on it, so it is filtered out here.

    Also rejected: a provider whose own summarizer degrades and echoes the
    literal first session message back as the "title". That message carries
    our injected context capsule (see `_build_prompt_prefix`), which is meant
    to stay invisible to the user - accepting it verbatim both leaked
    internal state into the sidebar and skipped the 6-word
    `_fallback_title` truncation, which only runs when no native title is
    accepted.
    """
    title = (title or "").strip()
    if not title or _PLACEHOLDER_TITLE_RE.match(title) or _INJECTED_CONTEXT_MARKER in title:
        return None
    return title


_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _fallback_title(user_text: str) -> str | None:
    """Deterministic fallback title derived from the user's first message.

    Used when the model call fails, so the
    sidebar never stays stuck on "New Chat" indefinitely.
    """
    snippet = (user_text or "").strip()
    if not snippet:
        return None
    # First line only, strip surrounding quotes.
    snippet = snippet.splitlines()[0].strip().strip('"').strip("'").strip()
    if not snippet:
        return None
    # A leading URL truncates mid-host ("Check Zendesk ticket https://scand…"),
    # which reads as broken text in the sidebar. Keep only the words before
    # the first URL when there are any; a bare-URL prompt keeps the URL.
    url_match = _URL_RE.search(snippet)
    if url_match and url_match.start() > 0:
        before = snippet[: url_match.start()].strip()
        if before:
            snippet = before
    # Cap at ~6 words or 60 chars.
    words = snippet.split()
    if len(words) > 6:
        snippet = " ".join(words[:6])
    snippet = snippet.rstrip(".!?:,")
    if len(snippet) > 60:
        snippet = snippet[:57].rstrip() + "..."
    return snippet or None


_TITLE_MAX_CHARS = 60


def _clean_llm_title(text: str | None) -> str | None:
    """Normalize a model-produced title, or None when it is not usable.

    Models answer with trailing newlines, wrapping quotes, or — when their
    own summarizer degrades — the literal first session message, which
    carries our injected ``[CIAO_CONTEXT_BEGIN]`` capsule. Accepting any of
    those would put them straight into the sidebar.
    """
    title = (text or "").strip()
    if not title:
        return None
    # First non-empty line only; the prompt asks for one line anyway.
    for line in title.splitlines():
        line = line.strip()
        if line:
            title = line
            break
    title = title.strip().strip('"').strip("'").strip("`").strip()
    title = title.rstrip(".!?:,")
    if not title or _PLACEHOLDER_TITLE_RE.match(title):
        return None
    if _INJECTED_CONTEXT_MARKER in title:
        return None
    if len(title) > _TITLE_MAX_CHARS:
        title = title[: _TITLE_MAX_CHARS - 3].rstrip() + "..."
    return title or None


_FRONTMATTER_DELIM = "---"

_DESCRIPTION_KEY_RE = re.compile(r"^description\s*:")


def _yaml_quote(value: str) -> str:
    """Encode *value* as a YAML double-quoted scalar.

    JSON string syntax is a subset of YAML's double-quoted style, so
    ``json.dumps`` already escapes the characters that break an unquoted
    scalar - colons, quotes, leading ``#``, newlines - without hand-rolling an
    encoder. ``ensure_ascii=False`` keeps accented descriptions readable in the
    file rather than exploding them into ``\\uXXXX``.
    """
    return json.dumps(value, ensure_ascii=False)


def _set_frontmatter_description(text: str, description: str) -> str | None:
    """Return *text* with its YAML frontmatter ``description:`` set.

    Surgical by design: every other line of the document, frontmatter included,
    survives byte-for-byte. Round-tripping the block through ``yaml.safe_dump``
    would reorder keys and strip the comments out of docs people hand-write.

    Creates the frontmatter block when the document has none - that block is
    what auto-discovery reads, so a doc without one cannot carry a description
    at all. Returns ``None`` when the frontmatter is open but never closed:
    that document is malformed, and guessing where the block ends risks
    rewriting prose.
    """
    quoted = f"description: {_yaml_quote(description)}"
    lines = text.split("\n")

    # The delimiter only opens frontmatter on line 1. Anywhere else it is a
    # horizontal rule in the body.
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        block = f"{_FRONTMATTER_DELIM}\n{quoted}\n{_FRONTMATTER_DELIM}\n"
        return f"{block}\n{text}" if text.strip() else block

    close = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == _FRONTMATTER_DELIM),
        None,
    )
    if close is None:
        return None

    start = next(
        (i for i in range(1, close) if _DESCRIPTION_KEY_RE.match(lines[i])),
        None,
    )
    if start is None:
        # Append rather than prepend: `name:`/`status:` conventionally lead the
        # block, and a new key at the bottom reads as the addition it is.
        return "\n".join(lines[:close] + [quoted] + lines[close:])

    # Consume the value's continuation lines. Block scalars (`description: |`)
    # and wrapped flow scalars both continue on more-indented lines, and a
    # blank line inside a block scalar is still part of the value.
    end = start + 1
    while end < close:
        line = lines[end]
        if line.strip() and not line[:1].isspace():
            break
        end += 1
    # A trailing run of blank lines separates keys; it belongs to whatever
    # comes next, not to the value we are replacing.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:start] + [quoted] + lines[end:])


def _normalize_chat_helper(value: Any) -> dict[str, Any]:
    """Fail closed on lifecycle metadata supplied by older or invalid clients."""
    if not isinstance(value, dict) or value.get("kind") != "proposal":
        return {}
    intent = str(value.get("intent") or "")
    policy = str(value.get("archive_policy") or "")
    if (intent, policy) not in {
        ("resolve", "when_resolved"),
        ("review", "manual"),
    }:
        return {}
    raw_ids = value.get("proposal_ids")
    if not isinstance(raw_ids, list):
        return {}
    proposal_ids = list(
        dict.fromkeys(
            item
            for item in raw_ids[:100]
            if isinstance(item, str) and 0 < len(item) <= 128
        )
    )
    if not proposal_ids:
        return {}
    return {
        "kind": "proposal",
        "intent": intent,
        "proposal_ids": proposal_ids,
        "archive_policy": policy,
    }


@dataclass(slots=True)
class ScheduleRunOutcome:
    completed: bool = False
    is_error: bool = False
    permission_requested: bool = False
    question_requested: bool = False
    stream_error: bool = False
    retry_pending: bool = False
    final_text: str = ""
    archived_to: str = ""
    # True when the run dispatched background subagents that had not finished
    # by the time we stopped waiting. Such a run is not "done" yet, so it must
    # stay visible rather than auto-archive on a half-complete result.
    subagents_pending: bool = False


def _schedule_run_clean(outcome: ScheduleRunOutcome) -> bool:
    return (
        outcome.completed
        and not outcome.is_error
        and not outcome.permission_requested
        and not outcome.question_requested
        and not outcome.stream_error
        and not outcome.retry_pending
        and not outcome.subagents_pending
    )


def _schedule_dispatch_status(outcome: ScheduleRunOutcome) -> tuple[str, str | None]:
    """Classify a scheduled turn for job-run history.

    A pending retry means the provider deferred the work, such as after a
    quota rejection. It remains unclean and visible, but is not an app error.
    Unsettled background subagents (or a run that ended on an interim message
    with no synthesis turn) mean the work is not done yet either — not a
    failure to report as such, but not a healthy run either: recording "ok"
    would clear a previous error while the follow-up never completed.
    """
    if outcome.retry_pending:
        return "skipped", None
    if outcome.stream_error or outcome.is_error:
        return "error", (outcome.final_text or "stream error")[:1000]
    if outcome.permission_requested or outcome.question_requested:
        return "skipped", None
    if outcome.subagents_pending:
        return "skipped", None
    return "ok", None


def _should_auto_archive_schedule_run(
    entry: object, outcome: ScheduleRunOutcome, *, needs_user: bool = False
) -> bool:
    archive_policy = getattr(entry, "archive_policy", "manual")
    if archive_policy != "auto":
        return False
    # Never auto-archive the chat an interval entry is bound to: archiving it
    # makes the next run fork a replacement and archive that too, forever. One
    # predicate, shared with the store-side normalisation that keeps `auto`
    # from being persisted for such an entry in the first place — two copies of
    # this rule would drift, and the dispatcher's copy is the one that decides.
    if not supports_auto_archive(entry):
        return False
    return _schedule_run_clean(outcome) and not needs_user
