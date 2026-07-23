"""Session transcript capture and markdown archival."""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    SDKSessionInfo,
    SessionMessage,
    delete_session,
    get_session_messages,
    get_subagent_messages,
    list_sessions,
    list_subagents,
)

from ciao.models import AgentRequest, ChatContext

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned or "session"


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


class TranscriptStore:
    """Persist in-progress provider sessions and archive them on demand."""

    def __init__(self, runtime_root: Path, archive_root: Path) -> None:
        self._runtime_root = runtime_root
        self._archive_root = archive_root
        self._v1_migrated = False

    def record_turn(
        self,
        request: AgentRequest,
        *,
        ctx: ChatContext,
        response_text: str,
        effective_model: str,
        session_id: str | None,
        usage: dict[str, str],
        quota: dict[str, str],
        input_kind: str,
        context_label: str = "",
        provider: str = "claude",
        tool_events: list[dict[str, Any]] | None = None,
        is_error: bool = False,
    ) -> None:
        transcript = self._load_current(ctx, provider)
        if not transcript:
            transcript = {
                "provider": provider,
                "started_at": _now_iso(),
                "selected_model": request.model,
                "session_id": session_id or request.resume_session or "",
                "context_key": ctx.key,
                "context_label": context_label,
                "turns": [],
            }
        if context_label and not transcript.get("context_label"):
            transcript["context_label"] = context_label
        transcript["updated_at"] = _now_iso()
        transcript["selected_model"] = request.model
        if session_id:
            transcript["session_id"] = session_id
        transcript.setdefault("turns", []).append(
            {
                "timestamp": _now_iso(),
                "input_kind": input_kind,
                "prompt": request.display_prompt or request.prompt,
                "mode": request.mode,
                "resume_session": request.resume_session or "",
                "image_count": len(request.images),
                "response": response_text,
                "is_error": is_error,
                "effective_model": effective_model or request.model,
                "usage": usage,
                "quota": quota,
                "tool_events": list(tool_events or []),
            }
        )
        self._save_current(ctx, transcript, provider)

    def archive_session(
        self,
        *,
        ctx: ChatContext,
        active_model: str,
        last_effective_model: str,
        session_id: str,
        provider: str = "claude",
    ) -> Path | None:
        transcript = self._load_current(ctx, provider)
        if not transcript or not transcript.get("turns"):
            return None
        ended_at = _now_iso()
        transcript["ended_at"] = ended_at
        transcript["active_model"] = active_model
        transcript["last_effective_model"] = last_effective_model or active_model
        transcript["session_id"] = transcript.get("session_id") or session_id
        body = self._render_markdown(transcript)
        archive_dir = self._archive_dir(ctx, provider)
        archive_dir.mkdir(parents=True, exist_ok=True)
        started_at = str(transcript.get("started_at") or ended_at).replace(":", "-")
        session_slug = _safe_slug(str(transcript.get("session_id") or "no-session-id"))
        path = archive_dir / f"{started_at}-{session_slug}.md"
        path.write_text(body, encoding="utf-8")
        self._delete_current(ctx, provider)
        return path

    def current_path(self, ctx: ChatContext, provider: str = "claude") -> Path:
        return self._current_path(ctx, provider)

    def delete_current(self, ctx: ChatContext, provider: str = "claude") -> None:
        """Delete an in-progress normalized transcript after an explicit delete."""

        self._delete_current(ctx, provider)

    def archive_dir(self, ctx: ChatContext, provider: str = "claude") -> Path:
        return self._archive_dir(ctx, provider)

    def peek_turn_count(self, ctx: ChatContext, provider: str = "claude") -> int:
        """Number of recorded turns in the current (pre-archive) transcript.

        Used by archive_chat to size-gate post-archive insights extraction
        before archive_session consumes the in-memory transcript file.
        """
        transcript = self._load_current(ctx, provider)
        turns = transcript.get("turns") if isinstance(transcript, dict) else None
        return len(turns) if isinstance(turns, list) else 0

    def current_messages(
        self, ctx: ChatContext, provider: str = "claude"
    ) -> list[dict[str, Any]]:
        """Render the durable in-progress transcript as PWA message rows."""
        transcript = self._load_current(ctx, provider)
        turns = transcript.get("turns") if isinstance(transcript, dict) else None
        if not isinstance(turns, list):
            return []
        rows: list[dict[str, Any]] = []
        for index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                continue
            timestamp = str(turn.get("timestamp") or "")
            prompt = str(turn.get("prompt") or "").strip()
            response = str(turn.get("response") or "").strip()
            if prompt:
                rows.append({
                    "role": "user",
                    "content": prompt,
                    "turn_index": index,
                    "sent_at": timestamp,
                })
            if response:
                row: dict[str, Any] = {
                    "role": "assistant",
                    "content": response,
                    "sent_at": timestamp,
                }
                if turn.get("is_error"):
                    row["is_error"] = True
                usage = turn.get("usage")
                if isinstance(usage, dict) and usage:
                    row["usage"] = usage
                quota = turn.get("quota")
                if isinstance(quota, dict) and quota:
                    row["quota"] = quota
                effective = str(turn.get("effective_model") or "")
                if effective:
                    row["effective_model"] = effective
                rows.append(row)
        return rows

    def current_filtered_jsonl(
        self, ctx: ChatContext, provider: str = "claude"
    ) -> str:
        """Return provider-neutral line JSON for insights and trajectories."""
        transcript = self._load_current(ctx, provider)
        turns = transcript.get("turns") if isinstance(transcript, dict) else None
        if not isinstance(turns, list):
            return ""
        lines: list[str] = []
        index = 0
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            prompt = str(turn.get("prompt") or "").strip()
            if prompt:
                lines.append(json.dumps({
                    "idx": index,
                    "type": "user",
                    "content": [{"type": "text", "text": prompt}],
                }, ensure_ascii=False))
                index += 1
            content: list[dict[str, Any]] = []
            response = str(turn.get("response") or "").strip()
            if response:
                content.append({"type": "text", "text": response})
            events = turn.get("tool_events")
            for event in events if isinstance(events, list) else []:
                if not isinstance(event, dict):
                    continue
                content.append({
                    "type": "tool_use",
                    "id": str(event.get("id") or ""),
                    "name": str(event.get("name") or "tool"),
                    "input": event.get("input") or {},
                })
            if content:
                lines.append(json.dumps({
                    "idx": index,
                    "type": "assistant",
                    "content": content,
                }, ensure_ascii=False))
                index += 1
        return "\n".join(lines)

    @staticmethod
    def delete_sdk_session_blob(workspace_root: Path, session_id: str) -> bool:
        """Delete the Claude Code SDK session JSONL blob for a session_id.

        Thin wrapper over :func:`claude_agent_sdk.delete_session` kept for
        call-site stability. Returns True if a session was deleted, False
        when the id is empty, the session was not found, or the SDK
        rejected the id (e.g., non-UUID).
        """
        if not session_id:
            return False
        try:
            delete_session(session_id, directory=str(workspace_root))
            return True
        except (FileNotFoundError, ValueError):
            return False
        except Exception:  # noqa: BLE001 — SDK may raise I/O errors
            logger.exception("delete_session failed for %s", session_id)
            return False

    # ── Global reads (for curation / weekly review) ───────────────────────

    def all_current_transcripts(self) -> list[tuple[str, str, dict]]:
        """Yield (context_key, provider, transcript_dict) across all contexts."""
        results: list[tuple[str, str, dict]] = []
        transcripts_root = self._runtime_root / "transcripts"
        if not transcripts_root.exists():
            return results
        for ctx_dir in transcripts_root.iterdir():
            if not ctx_dir.is_dir():
                continue
            for f in ctx_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    results.append((ctx_dir.name, f.stem, data))
                except (json.JSONDecodeError, OSError):
                    continue
        return results

    def all_archive_dirs(self) -> list[Path]:
        """All archive directories across all contexts."""
        results: list[Path] = []
        if not self._archive_root.exists():
            return results
        for ctx_dir in self._archive_root.iterdir():
            if not ctx_dir.is_dir():
                continue
            for provider_dir in ctx_dir.iterdir():
                if provider_dir.is_dir():
                    results.append(provider_dir)
        return results

    # ── Internal paths ────────────────────────────────────────────────────

    def _current_path(self, ctx: ChatContext, provider: str = "claude") -> Path:
        return self._runtime_root / "transcripts" / ctx.key / f"{_safe_slug(provider)}.json"

    def _archive_dir(self, ctx: ChatContext, provider: str = "claude") -> Path:
        return self._archive_root / ctx.key / _safe_slug(provider)

    def _load_current(self, ctx: ChatContext, provider: str = "claude") -> dict[str, Any]:
        path = self._current_path(ctx, provider)
        if not path.exists():
            self._maybe_migrate_v1()
            path = self._current_path(ctx, provider)
            if not path.exists():
                return {}
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return data
        except json.JSONDecodeError:
            return {}

    def _save_current(
        self, ctx: ChatContext, payload: dict[str, Any], provider: str = "claude"
    ) -> None:
        path = self._current_path(ctx, provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _delete_current(self, ctx: ChatContext, provider: str = "claude") -> None:
        path = self._current_path(ctx, provider)
        if path.exists():
            path.unlink()

    # ── V1 migration ──────────────────────────────────────────────────────

    def _maybe_migrate_v1(self) -> None:
        """Move flat .runtime/transcripts/<provider>.json → transcripts/default/."""
        if self._v1_migrated:
            return
        self._v1_migrated = True
        transcripts_root = self._runtime_root / "transcripts"
        if not transcripts_root.exists():
            return
        for f in transcripts_root.iterdir():
            if f.is_file() and f.suffix == ".json":
                dest_dir = transcripts_root / "default"
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest_dir / f.name))
        # Also migrate flat archive dirs
        if not self._archive_root.exists():
            return
        for provider_dir in self._archive_root.iterdir():
            if provider_dir.is_dir() and provider_dir.name == "claude":
                dest = self._archive_root / "default" / provider_dir.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.move(str(provider_dir), str(dest))

    # ── Markdown rendering ────────────────────────────────────────────────

    def _render_markdown(self, transcript: dict[str, Any]) -> str:
        turns = transcript.get("turns", [])
        usage_totals = self._usage_totals(turns)
        context_label = transcript.get("context_label", "")
        context_key = transcript.get("context_key", "")
        frontmatter = {
            "provider": transcript.get("provider", ""),
            "context": context_label or context_key or "",
            "selected_model": transcript.get("selected_model", ""),
            "active_model": transcript.get("active_model", ""),
            "last_effective_model": transcript.get("last_effective_model", ""),
            "session_id": transcript.get("session_id", ""),
            "started": transcript.get("started_at", ""),
            "ended": transcript.get("ended_at", ""),
            "turn_count": len(turns),
            "tags": ["transcript", str(transcript.get("provider", ""))],
            "usage_totals": usage_totals,
        }
        lines = ["---"]
        for key, value in frontmatter.items():
            lines.extend(self._yaml_lines(key, value))
        lines.extend(
            [
                "---",
                "",
                f"# Chat Transcript ({transcript.get('provider', '')})",
                "",
                f"- Started: {transcript.get('started_at', '-')}",
                f"- Ended: {transcript.get('ended_at', '-')}",
                f"- Selected model: {transcript.get('selected_model', '-')}",
                f"- Last effective model: {transcript.get('last_effective_model', '-')}",
                f"- Session id: {transcript.get('session_id', '-') or '-'}",
                "",
            ]
        )
        if usage_totals:
            lines.append("## Usage Totals")
            lines.append("")
            for key, value in usage_totals.items():
                lines.append(f"- {key}: {value}")
            lines.append("")
        for index, turn in enumerate(turns, start=1):
            lines.extend(
                [
                    f"## Turn {index}",
                    "",
                    f"- Time: {turn.get('timestamp', '-')}",
                    f"- Input kind: {turn.get('input_kind', '-')}",
                    f"- Mode: {turn.get('mode', '-')}",
                    f"- Effective model: {turn.get('effective_model', '-')}",
                    f"- Images: {turn.get('image_count', 0)}",
                    "",
                    "### User",
                    "",
                    "```text",
                    str(turn.get("prompt", "")),
                    "```",
                    "",
                    "### Assistant",
                    "",
                    "```text",
                    str(turn.get("response", "")),
                    "```",
                    "",
                ]
            )
            usage = turn.get("usage") or {}
            if usage:
                lines.append("### Usage")
                lines.append("")
                for key, value in usage.items():
                    lines.append(f"- {key}: {value}")
                lines.append("")
            quota = turn.get("quota") or {}
            if quota:
                lines.append("### Quota")
                lines.append("")
                for key, value in quota.items():
                    lines.append(f"- {key}: {value}")
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _usage_totals(self, turns: list[dict[str, Any]]) -> dict[str, int]:
        totals: dict[str, int] = {}
        for turn in turns:
            for key, value in (turn.get("usage") or {}).items():
                parsed = _coerce_int(value)
                if parsed is None:
                    continue
                totals[key] = totals.get(key, 0) + parsed
        return dict(sorted(totals.items()))

    def _yaml_lines(self, key: str, value: Any) -> list[str]:
        if isinstance(value, list):
            lines = [f"{key}:"]
            for item in value:
                lines.append(f"  - {item}")
            return lines
        if isinstance(value, dict):
            lines = [f"{key}:"]
            for subkey, subvalue in value.items():
                lines.append(f"  {subkey}: {subvalue}")
            return lines
        return [f"{key}: {value}"]


# ── CLI JSONL transcript extraction ──────────────────────────────────────


def _claude_projects_dir(workspace_root: Path) -> Path:
    """Derive the Claude Code session directory for a workspace."""
    # Claude Code encodes workspace path: /Users/me/ciao → -Users-me-ciao
    slug = str(workspace_root).replace("/", "-").lstrip("-")
    return Path.home() / ".claude" / "projects" / f"-{slug}"


def _peek_cli_entrypoint(path: Path) -> bool:
    """Return True if the session's first user entry has entrypoint == 'cli'.

    The SDK's ``get_session_messages()`` strips the JSONL envelope (timestamps,
    entrypoint, etc.) and returns only the inner Anthropic API message dicts.
    We still need the entrypoint to filter out bridge/SDK sessions that the
    web server already archives separately.
    """
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "user":
                    return bool(obj.get("entrypoint") == "cli")
    except OSError:
        return False
    return False


def _ms_to_iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return (
        datetime.fromtimestamp(ms / 1000, tz=UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _build_cli_turns(
    messages: list[SessionMessage],
) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    """Convert SDK session messages into our turn list.

    Extracts text blocks only (skips thinking, tool_use, tool_result), tallies
    assistant usage, and merges consecutive same-role turns so that tool-use
    chains collapse into a single assistant entry.
    """
    turns: list[dict[str, Any]] = []
    model = ""
    usage_totals: dict[str, int] = {}

    for sm in messages:
        msg = sm.message
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")

        if sm.type == "user":
            if isinstance(content, list):
                text_parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = "\n".join(text_parts)
                image_count = sum(
                    1
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "image"
                )
            else:
                text = str(content)
                image_count = 0
            if text.strip():
                turns.append({"role": "user", "text": text, "image_count": image_count})

        elif sm.type == "assistant":
            if not model:
                model = msg.get("model", "")

            text_parts = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
            elif isinstance(content, str):
                text_parts.append(content)
            response_text = "\n".join(text_parts).strip()
            if response_text:
                turns.append({"role": "assistant", "text": response_text})

            usage = msg.get("usage", {}) or {}
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                val = usage.get(key)
                if val is None:
                    continue
                try:
                    usage_totals[key] = usage_totals.get(key, 0) + int(val)
                except (TypeError, ValueError):
                    pass

    # Merge consecutive same-role turns (tool-use chains produce multiple
    # assistant entries between text replies).
    merged: list[dict[str, Any]] = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["text"] += "\n\n" + turn["text"]
        else:
            merged.append(dict(turn))

    return merged, model, usage_totals


def _fetch_subagent_transcripts(
    session_id: str,
    directory: str,
) -> list[dict[str, Any]]:
    """Fetch transcripts for subagents spawned inside a parent CLI session.

    Uses ``list_subagents`` (v0.1.60+) to discover subagent ids, then pulls
    each one's messages with ``get_subagent_messages``. The return value is a
    list of dicts shaped like the parent session's structured form, minus
    session-level metadata. Empty on SDK error, no subagents, or subagents
    with no text content.
    """
    try:
        agent_ids = list_subagents(session_id, directory=directory)
    except Exception:  # noqa: BLE001 — SDK I/O errors
        logger.exception("list_subagents failed for %s", session_id)
        return []

    subagents: list[dict[str, Any]] = []
    for agent_id in agent_ids:
        try:
            messages = get_subagent_messages(
                session_id,
                agent_id,
                directory=directory,
            )
        except Exception:  # noqa: BLE001 — SDK I/O errors
            logger.exception(
                "get_subagent_messages failed for %s/%s", session_id, agent_id
            )
            continue
        if not messages:
            continue
        turns, model, usage_totals = _build_cli_turns(messages)
        if not turns:
            continue
        subagents.append(
            {
                "agent_id": agent_id,
                "turns": turns,
                "model": model,
                "usage_totals": usage_totals,
            }
        )
    return subagents


def _parse_jsonl_session(
    path: Path,
    session_info: SDKSessionInfo,
) -> dict[str, Any] | None:
    """Build a structured session dict via the SDK for a CLI-entrypoint session.

    Returns None if the session is not a CLI session, the SDK fails to parse
    it, or it contains no text turns.
    """
    if not _peek_cli_entrypoint(path):
        return None

    session_id = session_info.session_id
    directory = session_info.cwd or str(path.parent)

    try:
        messages = get_session_messages(session_id, directory=directory)
    except Exception:  # noqa: BLE001 — SDK can raise a variety of I/O errors
        logger.exception("get_session_messages failed for %s", session_id)
        return None

    if not messages:
        return None

    turns, model, usage_totals = _build_cli_turns(messages)
    if not turns:
        return None

    return {
        "session_id": session_id,
        "entrypoint": "cli",
        "model": model,
        "started": _ms_to_iso(session_info.created_at),
        "ended": _ms_to_iso(session_info.last_modified),
        "turns": turns,
        "usage_totals": usage_totals,
        "git_branch": session_info.git_branch or "",
        "cwd": session_info.cwd or "",
        "subagents": _fetch_subagent_transcripts(session_id, directory),
    }


def _render_cli_transcript(session: dict[str, Any]) -> str:
    """Render a parsed CLI session as markdown matching the Telegram transcript format."""
    turns = session.get("turns", [])
    usage = session.get("usage_totals", {})
    subagents = session.get("subagents", []) or []

    # Count user turns (a "turn" in transcript convention = one user+assistant exchange)
    user_turns = [t for t in turns if t["role"] == "user"]

    frontmatter = {
        "type": "cli-transcript",
        "provider": "claude",
        "model": session.get("model", ""),
        "session_id": session.get("session_id", ""),
        "started": session.get("started", ""),
        "ended": session.get("ended", ""),
        "turn_count": len(user_turns),
        "subagent_count": len(subagents),
        "git_branch": session.get("git_branch", ""),
        "tags": ["cli", "transcript", "claude"],
        "usage_totals": usage,
    }

    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for sk, sv in value.items():
                lines.append(f"  {sk}: {sv}")
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", "", f"# CLI Transcript", ""])
    lines.extend([
        f"- Started: {session.get('started', '-')}",
        f"- Ended: {session.get('ended', '-')}",
        f"- Model: {session.get('model', '-')}",
        f"- Session id: {session.get('session_id', '-')}",
        f"- Git branch: {session.get('git_branch', '-')}",
        "",
    ])

    if usage:
        lines.append("## Usage Totals")
        lines.append("")
        for key, value in sorted(usage.items()):
            lines.append(f"- {key}: {value}")
        lines.append("")

    turn_num = 0
    for turn in turns:
        if turn["role"] == "user":
            turn_num += 1
            lines.extend([
                f"## Turn {turn_num}",
                "",
                "### User",
                "",
                "```text",
                turn["text"],
                "```",
                "",
            ])
        elif turn["role"] == "assistant":
            lines.extend([
                "### Assistant",
                "",
                "```text",
                turn["text"],
                "```",
                "",
            ])

    if subagents:
        lines.extend(["## Subagents", ""])
        for sub in subagents:
            agent_id = sub.get("agent_id", "?")
            sub_model = sub.get("model", "")
            sub_usage = sub.get("usage_totals", {}) or {}
            lines.append(f"### Subagent `{agent_id}`")
            lines.append("")
            if sub_model:
                lines.append(f"- Model: {sub_model}")
            if sub_usage:
                usage_str = ", ".join(
                    f"{k}: {v}" for k, v in sorted(sub_usage.items())
                )
                lines.append(f"- Usage: {usage_str}")
            lines.append("")

            sub_turn_num = 0
            for turn in sub.get("turns", []):
                if turn["role"] == "user":
                    sub_turn_num += 1
                    lines.extend([
                        f"#### Turn {sub_turn_num}",
                        "",
                        "##### User",
                        "",
                        "```text",
                        turn["text"],
                        "```",
                        "",
                    ])
                elif turn["role"] == "assistant":
                    lines.extend([
                        "##### Assistant",
                        "",
                        "```text",
                        turn["text"],
                        "```",
                        "",
                    ])

    return "\n".join(lines).rstrip() + "\n"


def extract_cli_transcripts(
    workspace_root: Path,
    archive_root: Path,
    tracking_path: Path,
) -> list[Path]:
    """Extract new CLI JSONL sessions to readable .md transcripts.

    Uses the Claude Agent SDK's session APIs (``list_sessions`` and
    ``get_session_messages``) to walk sessions in chronological order. The
    SDK rebuilds the conversation via ``parentUuid`` links, which is more
    correct than file-order parsing for branched/forked sessions.

    Args:
        workspace_root: Project workspace root (e.g. /Users/me/ciao).
        archive_root: Where to write transcripts (e.g. memory-vault/Logs/CLI/).
        tracking_path: JSON file tracking already-extracted session IDs.

    Returns:
        List of paths to newly created transcript files.
    """
    projects_dir = _claude_projects_dir(workspace_root)
    if not projects_dir.is_dir():
        return []

    # Load tracking state
    extracted: set[str] = set()
    if tracking_path.exists():
        try:
            extracted = set(json.loads(tracking_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass

    try:
        sessions = list_sessions(
            directory=str(workspace_root),
            include_worktrees=False,
        )
    except Exception:  # noqa: BLE001 — SDK can raise a variety of I/O errors
        logger.exception("list_sessions failed for %s", workspace_root)
        return []

    created: list[Path] = []
    for sess in sessions:
        session_id = sess.session_id
        if session_id in extracted:
            continue

        jsonl_path = projects_dir / f"{session_id}.jsonl"
        session = _parse_jsonl_session(jsonl_path, sess)
        if session is None:
            # Not a CLI session or empty: mark as seen so we don't re-check
            extracted.add(session_id)
            continue

        body = _render_cli_transcript(session)
        archive_root.mkdir(parents=True, exist_ok=True)
        started = str(session.get("started", "")).replace(":", "-")
        out_path = archive_root / f"{started}-{session_id}.md"
        out_path.write_text(body, encoding="utf-8")
        extracted.add(session_id)
        created.append(out_path)
        logger.info("Extracted CLI transcript: %s", out_path.name)

    # Save tracking state
    tracking_path.parent.mkdir(parents=True, exist_ok=True)
    tracking_path.write_text(
        json.dumps(sorted(extracted), indent=2),
        encoding="utf-8",
    )

    return created
