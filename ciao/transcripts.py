"""Session transcript capture and markdown archival."""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    SessionMessage,
    delete_session,
    get_session_messages,
    get_session_messages as _sdk_get_session_messages,
)

from ciao.jsonio import read_json_dict
from ciao.models import AgentRequest, ChatContext

logger = logging.getLogger(__name__)

# Turn-journal flush cadence: buffered event records spill to disk when this
# many seconds have elapsed since the last write or the buffer grows past the
# entry cap, whichever comes first. A crash loses at most this much tail.
_JOURNAL_FLUSH_SECONDS = 0.25
_JOURNAL_FLUSH_ENTRIES = 32


class TurnJournal:
    """Append-only crash journal for one in-flight turn.

    The normalized transcript (``record_turn``) is written only at end of
    turn, so a server crash or provider abort mid-turn previously lost the
    whole exchange. The journal mirrors the stream's user-visible events as
    JSON lines while the turn runs; on normal completion ``finish()`` deletes
    the file. A file left behind by a crash is folded back into the transcript
    as an ``is_partial`` turn by :meth:`TranscriptStore.recover_journals`.

    Writes are synchronous on purpose: cancellation cannot interrupt them
    mid-line, so no shield wrapper is needed around finalization.
    """

    def __init__(self, journal_dir: Path, provider: str) -> None:
        self._dir = journal_dir
        self._provider = provider
        self._path: Path | None = None
        self._handle: Any = None
        self._buffer: list[str] = []
        self._last_flush = 0.0
        # The elapsed deadline used to be checked only from `append()`, so a
        # turn that emitted a short burst and then went quiet held those records
        # until the next one arrived - a crash in the quiet stretch lost an
        # arbitrarily old reply rather than the documented 250ms tail. A timer
        # makes the deadline real; it fires on its own thread, so the buffer and
        # the handle are guarded.
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def begin(self, header: dict[str, Any]) -> None:
        """Create the journal file and write the header record.

        Failures are log-only: a broken journal must never break the turn.
        """
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            name = f"{_safe_slug(self._provider)}-{stamp}-{id(self):x}.jsonl"
            self._path = self._dir / name
            self._handle = self._path.open("a", encoding="utf-8")
            self._last_flush = time.monotonic()
            # Header goes straight to disk so a crash before any event still
            # leaves a recoverable prompt + provider record.
            self._handle.write(
                json.dumps({"type": "begin", **header}, ensure_ascii=False) + "\n"
            )
            self._handle.flush()
        except OSError:
            logger.exception("Failed opening turn journal under %s", self._dir)
            self._handle = None

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            if self._handle is None:
                return
            self._buffer.append(json.dumps(record, ensure_ascii=False))
            now = time.monotonic()
            if (
                len(self._buffer) >= _JOURNAL_FLUSH_ENTRIES
                or now - self._last_flush >= _JOURNAL_FLUSH_SECONDS
            ):
                self._flush_locked()
            else:
                self._arm_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _arm_locked(self) -> None:
        """Make sure the buffered tail lands even if nothing else arrives."""
        if self._timer is not None:
            return
        delay = max(0.0, _JOURNAL_FLUSH_SECONDS - (time.monotonic() - self._last_flush))
        timer = threading.Timer(delay, self._flush_from_timer)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _flush_from_timer(self) -> None:
        with self._lock:
            self._timer = None
            self._flush_locked()

    def _flush_locked(self) -> None:
        self._cancel_locked()
        if self._handle is None or not self._buffer:
            return
        try:
            self._handle.write("\n".join(self._buffer) + "\n")
            self._handle.flush()
        except OSError:
            logger.exception("Failed writing turn journal %s", self._path)
        finally:
            self._buffer.clear()
            self._last_flush = time.monotonic()

    def mark_committed(self) -> None:
        """Record that the normalized turn is durably written.

        ``record_turn`` and ``finish`` are two steps, and a process death
        between them left behind a journal whose turn was ALREADY in the
        transcript - recovery then folded it in a second time, duplicating the
        prompt and the reply into history, the archive and the insights input,
        after exactly the crash the journal exists to survive. A journal
        carrying this marker is dropped by recovery instead of replayed.
        """
        with self._lock:
            if self._handle is None:
                return
            self._buffer.append(json.dumps({"type": "committed"}, ensure_ascii=False))
            self._flush_locked()

    def finish(self) -> None:
        """Close and delete the journal — the turn completed normally."""
        with self._lock:
            self._flush_locked()
            self._finish_locked()

    def _finish_locked(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        if self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed removing turn journal %s", self._path)
            self._path = None


def _journal_event_record(event: Any) -> dict[str, Any] | None:
    """Map a stream event to its compact journal record (None = skip)."""
    type_name = type(event).__name__
    if type_name == "AssistantTextDelta":
        text = getattr(event, "text", "")
        return {"type": "text", "text": text} if text else None
    if type_name == "ToolUseEvent":
        name = getattr(event, "tool_name", "")
        return {"type": "tool", "name": name} if name else None
    if type_name == "ResultEvent":
        return {"type": "result", "is_error": bool(getattr(event, "is_error", False))}
    # Thinking/system/permission events carry no recoverable reply content.
    return None


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# The injected-context envelope stays in the stored turns — chat recovery
# parses its [CONTEXT]/[Project] lines to re-home a transcript — and is only
# stripped when turns are rendered as visible chat rows.
_INJECTED_CONTEXT_RE = re.compile(r"(?s)^\[CIAO_CONTEXT_BEGIN\].*?\[CIAO_CONTEXT_END\]\s*")


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
        is_partial: bool = False,
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
                # A force-stopped turn is durable but incomplete; renderers
                # already treat this flag as "the reply was cut short".
                **({"is_partial": True} if is_partial else {}),
            }
        )
        self._save_current(ctx, transcript, provider)

    def open_turn_journal(self, ctx: ChatContext, provider: str = "claude") -> TurnJournal:
        """Create a crash journal for one in-flight turn of this chat."""
        return TurnJournal(
            self._runtime_root / "transcripts" / ctx.key / "journal", provider
        )

    def record_partial_turn(
        self,
        ctx: ChatContext,
        *,
        provider: str,
        prompt: str,
        response_text: str,
        tool_events: list[dict[str, Any]] | None = None,
        started_at: str = "",
        journal_path: Path | None = None,
    ) -> bool:
        """Append an ``is_partial`` turn recovered from a crash journal.

        Returns whether a turn was actually appended, so the caller's count
        reports recoveries rather than journals seen.

        Idempotent on the journal's own name. `_save_current` and the unlink
        below are two steps: a death between them - or an unlink that simply
        raises - leaves a journal whose turn is ALREADY durable, and the next
        startup folded it in again, duplicating the prompt and the reply after
        exactly the crash recovery exists to survive.

        A `committed` marker (as the normal turn path uses) would only narrow
        that window, because a death before the marker still replays a durable
        turn. Stamping the turn with the journal filename - which is unique per
        turn - and checking for it first closes the window wherever the crash
        lands.
        """
        source = journal_path.name if journal_path is not None else ""
        transcript = self._load_current(ctx, provider)
        if source and transcript:
            for turn in transcript.get("turns") or []:
                if turn.get("recovered_from") == source:
                    # Already folded in by an earlier startup; this journal only
                    # outlived its own unlink.
                    self._drop_journal(journal_path)
                    return False
        if not transcript:
            transcript = {
                "provider": provider,
                "started_at": started_at or _now_iso(),
                "selected_model": "",
                "session_id": "",
                "context_key": ctx.key,
                "turns": [],
            }
        transcript["updated_at"] = _now_iso()
        transcript.setdefault("turns", []).append(
            {
                "timestamp": started_at or _now_iso(),
                "input_kind": "recovered",
                "prompt": prompt,
                "mode": "",
                "resume_session": "",
                "image_count": 0,
                "response": response_text,
                "is_error": False,
                "is_partial": True,
                "effective_model": "",
                "usage": {},
                "quota": {},
                "tool_events": list(tool_events or []),
                # The journal this turn came from, so a replay can recognise it.
                "recovered_from": source,
            }
        )
        self._save_current(ctx, transcript, provider)
        self._drop_journal(journal_path)
        return True

    def _drop_journal(self, journal_path: Path | None) -> None:
        if journal_path is None:
            return
        try:
            journal_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed removing recovered journal %s", journal_path)

    def recover_journals(self) -> int:
        """Fold journals left behind by crashed turns into their transcripts.

        Runs once at startup. Each leftover journal becomes one ``is_partial``
        turn (recovered prompt + streamed text + tool names); the journal file
        is deleted after a successful fold. Returns the number recovered.
        """
        journals_root = self._runtime_root / "transcripts"
        if not journals_root.exists():
            return 0
        recovered = 0
        for journal_file in sorted(journals_root.glob("*/journal/*.jsonl")):
            try:
                provider = "claude"
                prompt = ""
                started_at = ""
                committed = False
                texts: list[str] = []
                tool_events: list[dict[str, Any]] = []
                with journal_file.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        kind = record.get("type")
                        if kind == "begin":
                            provider = str(record.get("provider") or "claude")
                            prompt = str(record.get("prompt") or "")
                            started_at = str(record.get("started_at") or "")
                        elif kind == "text":
                            texts.append(str(record.get("text") or ""))
                        elif kind == "tool":
                            tool_events.append({
                                "id": "",
                                "name": str(record.get("name") or "tool"),
                                "input": {"summary": ""},
                            })
                        elif kind == "committed":
                            committed = True
                if committed:
                    # The turn is already in the transcript; this journal only
                    # outlived the unlink. Replaying it would duplicate the
                    # exchange, so drop it and move on.
                    journal_file.unlink(missing_ok=True)
                    continue
                ctx = ChatContext(chat_id=0, key_override=journal_file.parent.parent.name)
                appended = self.record_partial_turn(
                    ctx,
                    provider=provider,
                    prompt=prompt,
                    response_text="".join(texts).strip(),
                    tool_events=tool_events,
                    started_at=started_at,
                    journal_path=journal_file,
                )
                # Only a real append counts: a journal that outlived its own
                # unlink is dropped, not recovered, and reporting it would
                # claim a turn was restored twice.
                recovered += 1 if appended else 0
            except (OSError, json.JSONDecodeError):
                logger.exception("Failed recovering turn journal %s", journal_file)
        if recovered:
            logger.info("Recovered %d partial turn(s) from crash journals", recovered)
        return recovered

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
            prompt = _INJECTED_CONTEXT_RE.sub("", str(turn.get("prompt") or "")).strip()
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
                if turn.get("is_partial"):
                    row["partial"] = True
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
            data = read_json_dict(path)
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


# The global session-lookup fallback (``~/.claude/projects/*/<sid>.jsonl``)
# walks every project slug dir. On a machine with a few hundred slugs that is
# hundreds of opendir calls per probe, and probes arrive on the event loop
# from polling endpoints — a multi-second scandir storm that stalls every
# in-flight stream (observed: 238 stale eval dirs, ~10ms per glob, loop wedged
# for minutes). Cache the *slug directory list* (one cheap iterdir of the
# projects root) and resolve each probe with one O(1) stat per slug dir —
# no per-dir content enumeration, so a session file created moments ago is
# found immediately. A brand-new cwd gets its slug dir on the next refresh,
# which a session-id miss forces (rate-limited so absent-session polling
# costs at most one extra iterdir per interval): callers treat a lookup miss
# as final — the subagent completion watcher exits without retrying, and the
# schedule wait reports the agents settled — so a miss must never be served
# from a listing that cannot contain the session.
_GLOBAL_SESSION_SCAN_TTL = 30.0
_GLOBAL_SESSION_RESCAN_MIN_INTERVAL = 2.0
# Serializes the slug walk so N concurrent probes cost one walk, and
# coalesces miss-triggered refreshes the same way.
_global_session_scan_lock = threading.Lock()
# Keyed by the projects root so a changed home() (tests, relocatable homes)
# invalidates naturally instead of serving another tree's listing. The float
# is the monotonic time the slug list was captured; it also gates the
# miss-triggered refresh.
_global_session_scan_cache: tuple[Path, float, list[Path]] | None = None


def _global_session_slugs_fresh(
    projects_root: Path, now: float
) -> list[Path] | None:
    """The cached slug-dir list when fresh for ``projects_root``, else None."""
    cached = _global_session_scan_cache
    if (
        cached is not None
        and cached[0] == projects_root
        and now - cached[1] < _GLOBAL_SESSION_SCAN_TTL
    ):
        return cached[2]
    return None


def _global_session_slugs_locked(
    projects_root: Path, now: float
) -> list[Path]:
    """Re-walk ``projects_root`` and refresh the slug list. Lock is held.

    A failed walk keeps the previous list (even a stale one) rather than
    poisoning the cache with an empty result over a transient OSError.
    """
    global _global_session_scan_cache
    slugs: list[Path] = []
    try:
        for entry in projects_root.iterdir():
            if entry.is_dir():
                slugs.append(entry)
    except OSError:
        cached = _global_session_scan_cache
        if cached is not None and cached[0] == projects_root:
            return cached[2]
        return []
    _global_session_scan_cache = (projects_root, now, slugs)
    return slugs


def _global_session_matches(
    session_id: str, *, force_refresh: bool = False
) -> list[Path]:
    """Cross-cwd paths for ``<session_id>.jsonl``, always fresh at file level.

    The slug-dir list is cached (one shared iterdir per TTL window); each
    lookup stats ``<slug>/<sid>.jsonl`` directly, so a session created under
    an existing cwd mid-window is found on the next probe. On a total miss
    the slug list is refreshed once per rescan interval — a brand-new cwd
    gets picked up without absent-session polling rebuilding the per-probe
    scandir storm this cache exists to prevent.

    ``force_refresh`` bypasses that interval. Callers whose single lookup
    miss is final (the subagent completion watcher stops tracking; the
    schedule wait reports the agents settled) must pass True so the gate
    cannot suppress their one decisive probe; high-frequency pollers keep
    the default and absorb the bounded window.
    """
    if not session_id:
        return []
    target = f"{session_id}.jsonl"
    projects_root = Path.home() / ".claude" / "projects"
    now = time.monotonic()
    slugs = _global_session_slugs_fresh(projects_root, now)
    if slugs is None:
        with _global_session_scan_lock:
            now = time.monotonic()
            slugs = _global_session_slugs_fresh(projects_root, now)
            if slugs is None:
                slugs = _global_session_slugs_locked(projects_root, now)
        if not slugs:
            return []
    matches = [slug / target for slug in slugs if (slug / target).exists()]
    if matches:
        return matches
    # Miss. A session created in a slug dir the cached list does not know
    # yet is only reachable after a refresh; coalesce and rate-limit it
    # unless the caller cannot tolerate a suppressed miss.
    with _global_session_scan_lock:
        now = time.monotonic()
        entry = _global_session_scan_cache
        if (
            not force_refresh
            and entry is not None
            and now - entry[1] < _GLOBAL_SESSION_RESCAN_MIN_INTERVAL
        ):
            # The list was captured moments ago (cold walk or a concurrent
            # refresh); a new slug dir cannot have appeared inside the
            # window often enough to justify another walk per probe.
            return []
        slugs = _global_session_slugs_locked(projects_root, now)
        return [slug / target for slug in slugs if (slug / target).exists()]


def _global_session_candidates() -> list[Path]:
    """One cached pass over ``~/.claude/projects/*/`` returning *.jsonl paths.

    Kept for callers that want the full unfiltered listing; session lookups
    should prefer :func:`_global_session_matches`, which stays file-fresh
    via per-slug stats and refreshes the slug list on a miss. Callers may
    ``stat`` a candidate before trusting it.
    """
    projects_root = Path.home() / ".claude" / "projects"
    now = time.monotonic()
    slugs = _global_session_slugs_fresh(projects_root, now)
    if slugs is None:
        with _global_session_scan_lock:
            now = time.monotonic()
            slugs = _global_session_slugs_fresh(projects_root, now)
            if slugs is None:
                slugs = _global_session_slugs_locked(projects_root, now)
    candidates: list[Path] = []
    for slug in slugs or []:
        try:
            candidates.extend(slug.glob("*.jsonl"))
        except OSError:
            continue
    return candidates


def find_claude_session_file(
    session_id: str,
    workspace_root: Path | str,
    *,
    agent_root: Path | str | None = None,
    force_refresh: bool = False,
) -> Path | None:
    """Locate ``<session_id>.jsonl`` for a Claude session on this machine.

    Checks the workspace slug dir first, then falls back to the cached scan
    over ``~/.claude/projects`` for sessions recorded under a different cwd
    (see :func:`_global_session_matches` for the freshness contract and when
    ``force_refresh`` is required). Returns the path (not verified to be
    non-empty) or None.
    """
    if not session_id:
        return None
    root = Path(agent_root) if agent_root is not None else Path(workspace_root)
    preferred = _claude_projects_dir(root) / f"{session_id}.jsonl"
    if preferred.exists():
        return preferred
    matches = _global_session_matches(session_id, force_refresh=force_refresh)
    return matches[0] if matches else None


def get_session_messages_full(
    session_id: str,
    directory: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[SessionMessage]:
    """Reads a session's conversation messages from its JSONL transcript file,
    stitching history across autocompact / compact_boundary entries by following
    both `parentUuid` and `logicalParentUuid`.
    """
    import sys

    def _fallback(gsm: Any = get_session_messages) -> list[SessionMessage]:
        # Prefer a caller-supplied getter so tests that replace
        # sys.modules["claude_agent_sdk"] still reach their mock instead of the
        # statically imported SDK binding from module import time.
        #
        # The getter is Any (it may be an SDK binding or a test double), so its
        # result is cast rather than inferred - without that the Any leaks out
        # through this function's callers and mypy rejects the return.
        if limit is None and offset == 0:
            return cast("list[SessionMessage]", gsm(session_id, directory=directory))
        try:
            return cast(
                "list[SessionMessage]",
                gsm(session_id, directory=directory, limit=limit, offset=offset),
            )
        except TypeError:
            return cast("list[SessionMessage]", gsm(session_id, directory=directory))

    if get_session_messages is not _sdk_get_session_messages:
        return _fallback()

    sdk = sys.modules.get("claude_agent_sdk")
    if sdk and hasattr(sdk, "get_session_messages") and not hasattr(sdk, "_internal"):
        return _fallback(sdk.get_session_messages)

    try:
        from claude_agent_sdk._internal.sessions import (
            _is_visible_message,
            _parse_transcript_entries,
            _read_session_file,
            _to_session_message,
            _validate_uuid,
        )
    except (ImportError, AttributeError):
        return get_session_messages(session_id, directory=directory, limit=limit, offset=offset)

    if not _validate_uuid(session_id):
        return []

    try:
        content = _read_session_file(session_id, directory)
    except Exception:
        content = None

    if not content:
        return get_session_messages(session_id, directory=directory, limit=limit, offset=offset)

    try:
        entries = _parse_transcript_entries(content)
        if not entries:
            return []

        by_uuid = {e["uuid"]: e for e in entries if "uuid" in e and isinstance(e["uuid"], str)}
        entry_index = {e["uuid"]: i for i, e in enumerate(entries) if "uuid" in e and isinstance(e["uuid"], str)}

        parent_uuids: set[str] = set()
        for e in entries:
            p = e.get("parentUuid")
            if p and isinstance(p, str):
                parent_uuids.add(p)

        terminals = [
            e for e in entries
            if "uuid" in e and isinstance(e["uuid"], str) and e["uuid"] not in parent_uuids
        ]

        leaves: list[dict] = []
        for terminal in terminals:
            cur: dict | None = terminal
            seen: set[str] = set()
            while cur is not None:
                uid = cur.get("uuid")
                if not uid or not isinstance(uid, str) or uid in seen:
                    break
                seen.add(uid)
                if cur.get("type") in ("user", "assistant"):
                    leaves.append(cur)
                    break
                parent = cur.get("parentUuid") or cur.get("logicalParentUuid")
                cur = by_uuid.get(parent) if parent and isinstance(parent, str) else None

        if not leaves:
            return []

        main_leaves = [
            leaf
            for leaf in leaves
            if not leaf.get("isSidechain")
            and not leaf.get("teamName")
            and not leaf.get("isMeta")
        ]

        def _pick_best(candidates: list[dict]) -> dict:
            best = candidates[0]
            best_idx = entry_index.get(best.get("uuid", ""), -1)
            for item in candidates[1:]:
                cur_idx = entry_index.get(item.get("uuid", ""), -1)
                if cur_idx > best_idx:
                    best = item
                    best_idx = cur_idx
            return best

        leaf = _pick_best(main_leaves) if main_leaves else _pick_best(leaves)

        chain: list[dict] = []
        seen = set()
        cur = leaf
        while cur is not None:
            uid = cur.get("uuid")
            if not uid or not isinstance(uid, str) or uid in seen:
                break
            seen.add(uid)
            chain.append(cur)
            parent = cur.get("parentUuid") or cur.get("logicalParentUuid")
            cur = by_uuid.get(parent) if parent and isinstance(parent, str) else None

        chain.reverse()
        visible = [e for e in chain if _is_visible_message(e)]
        messages: list[SessionMessage] = []
        for e in visible:
            msg = _to_session_message(e)
            if e.get("isCompactSummary") and isinstance(msg.message, dict):
                msg.message["isCompactSummary"] = True
            messages.append(msg)

        if limit is not None and limit > 0:
            return messages[offset : offset + limit]
        if offset > 0:
            return messages[offset:]
        return messages
    except Exception:
        logger.exception("get_session_messages_full custom chain failed for %s; falling back", session_id)
        return get_session_messages(session_id, directory=directory, limit=limit, offset=offset)

