"""Post-archive session reading: the transcript filter, the retry policy, and
the archive pipeline's one stage.

When a chat is archived, the user/assistant text turns are rendered to
``memory-vault/Logs/Chats/<context>/claude/<file>.md`` by
``TranscriptStore.archive_session``. That renderer drops everything that
isn't plain text: tool_use, tool_result, thinking blocks, errors, retries.
:func:`filter_session_jsonl` mines the raw Claude Code session JSONL (at
``~/.claude/projects/-home-ubuntu-ciao/<session-id>.jsonl``) for the signal
those layers contain, so the trajectory built from it sees the same picture
the renderer does.

Three things live here, and they share one reason to: they are the pieces of
archive-time session reading with no other home.

* :func:`filter_session_jsonl` runs synchronously inside ``archive_chat``
  before ``delete_sdk_session_blob`` removes the JSONL from disk. It reads the
  file, drops noise, truncates large read-only tool_result bodies, and flags
  the unattended turns of a system-schedule run.
* :func:`call_with_retry` (with :class:`RetryOutcome`,
  :func:`is_context_overflow` and :func:`is_terminal_failure`) is the one
  retry policy every one-shot in the app shares, so a context-window overflow
  and an auth rejection are never both answered with "try again".
* :func:`run_archive_pipeline` is the manifest runner. Its one stage is the
  trajectory; the memory pass, a chat of the app's own, owns everything that
  writes to the vault.

:func:`locate_insights_section` and :func:`_has_insights_section` read the
``## Session insights`` section an older build appended. Nothing appends one
any more, but :mod:`ciao.archive_jobs` authenticates a crashed append from
before this change against them, so a manifest written then still resumes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ciao import job_runs
from ciao.memory_policy import UNATTENDED_MARKER as _UNATTENDED_MARKER

if TYPE_CHECKING:
    from ciao.config import CiaoConfig
from ciao.transcripts import _claude_projects_dir

logger = logging.getLogger(__name__)


def resolve_insights_model(
    config: CiaoConfig, workspace: str | None = None, provider: str | None = None
) -> str:
    """Pick the model for session-insights extraction.

    When the operator has not set an explicit override (Settings → Models →
    Session insights = Automatic), use the workspace/provider default model.
    Scripts without either context fall back to ``config.insights_model``.

    ``provider``, when given, is the chat's actual provider; it is passed
    through to ``default_model_for_workspace`` so an opencode chat in a
    Claude-default workspace resolves that provider's own default model
    instead of a Claude tier alias.
    """
    if config.insights_model_override:
        return config.insights_model_override
    if workspace is not None or provider is not None:
        return config.default_model_for_workspace(workspace, provider)
    return config.insights_model


# The capsule marker an unattended (schedule/automation) turn carries in its
# injected context. It survives into the raw session JSONL as part of the user
# message, so extraction can tell a real user turn from the machinery that
# fired it. The value lives in ciao/memory_policy.py with the rest of the
# policy, so the capsule and the extractor cannot disagree on the marker.
_INSIGHTS_HEADER = "## Session insights"
# Written immediately before the header by the removed insights stage, so the real
# appended section is distinguishable from a transcript that merely quotes
# the header text (curation chats do this routinely). Archives written
# before the stamp existed are handled by the heuristic in
# locate_insights_section.
_INSIGHTS_STAMP = "<!-- ciao:session-insights -->"
_MARKER_LINE_RE = re.compile(
    rf"^{re.escape(_INSIGHTS_HEADER)}[ \t]*(?=\r?$)", re.MULTILINE
)
# The stamp is part of the archive format too: require it at the beginning of
# its own line before binding it to the following heading. Without that
# anchor, a transcript sentence ending in the stamp could bind to a heading
# on the next line and make the quoted text look like extracted insights.
_STAMPED_MARKER_RE = re.compile(
    rf"^{re.escape(_INSIGHTS_STAMP)}\r?\n"
    rf"{re.escape(_INSIGHTS_HEADER)}[ \t]*(?=\r?$)",
    re.MULTILINE,
)
# Rendered archives title each transcript turn "## Turn N", render subagent
# turns as "#### Turn N" under a trailing "## Subagents" block, and close with
# "### Usage"/"### Quota" trailers. The appended insights section always sits
# after all of those, so any such heading after a marker proves the marker is
# quoted transcript content.
_TURN_HEADING_RE = re.compile(
    r"^(?:#{2,4} Turn \d+|## Subagents\s*$|### (?:Usage|Quota)\s*$)",
    re.MULTILINE,
)
# Rendered archives fence quoted transcript text in line-start ``` blocks
# (the appended body's snippet fences are indented and do not match).
_FENCE_LINE_RE = re.compile(r"^```", re.MULTILINE)
_RETRY_DELAY_S = 30
_READ_TOOL_TRUNCATE_CHARS = 200
_KEEP_FULL_TOOLS = frozenset({"Edit", "Write", "Bash", "Task", "NotebookEdit"})
_TRUNCATE_TOOLS = frozenset({"Read", "Glob", "Grep", "WebFetch", "WebSearch"})

# The insights model is operator-chosen and may be a slow local/cloud GGUF:
# measured end-to-end calls on such a backend run 214-253s, so the old flat
# 120s budget turned tail latency into a guaranteed TimeoutError and the job
# failed ~79% of the time. Generous on purpose.
_DEFAULT_TIMEOUT_S = 600.0

# Per-transcript input ceiling. Long sessions otherwise exceed the model's
# context window outright (observed: 131k / 200k / 262k tokens against a
# 125,952-token window), and a flat retry re-sends the same oversized payload
# and fails identically. Chars, not tokens, because we cannot tokenize for an
# arbitrary backend; ~3.5 chars/token puts this near 90k tokens and leaves
# headroom for the system prompt.
_DEFAULT_MAX_INPUT_CHARS = 320_000


# Cap on the fact-augmented "Known context" block prepended to the extraction
# prompt. Small by design: it is reference data (current region entries plus
# an entity roster), not a second transcript.


# Excerpts of the notes this session's entities already have. Capped apart
# from the roster above so a long roster can never crowd them out, and small:
# enough for the model to see what a note already says, not the note itself.


def _resolve_insights_call(
    config, model: str, *, provider: str = "claude"
) -> tuple[str, str, str | None]:
    """Resolve an insights model to (effective_model, provider, note).

    The requested model is used as-is; ``note`` is always ``None`` now that no
    model is substituted, and stays in the shape for the callers that log it.
    """
    # Routine settings qualify runtime-provider overrides so a global choice
    # is not accidentally sent through Claude (the default one-shot provider).
    for routed_provider in ("opencode",):
        prefix = f"{routed_provider}:"
        if model.startswith(prefix):
            return model[len(prefix):] or "sonnet", routed_provider, None

    return model, provider, None


def is_context_overflow(exc: Exception) -> bool:
    """True for a deterministic oversized-input rejection.

    These fail identically on retry, so re-sending only burns another slow
    call plus the retry wait. Matched on message text because the providers
    surface it as a plain 400 rather than a typed error. Reused by the
    schedule attention classifier so the two callers classify 400s the
    same way.
    """
    text = str(exc).lower()
    return "too long" in text or "context window" in text or "context_length_exceeded" in text


def is_terminal_failure(exc: Exception) -> bool:
    """True when the provider already classified the failure as non-retriable.

    ``ciao.providers.oneshot`` sets ``OneShotError.transient`` from the
    upstream status and body: auth, subscription, quota, usage-limit and
    bad-model rejections fail identically on a second call, and
    ``run_oneshot`` therefore raises them without retrying internally.
    Re-sending them from here only buys another rejected request plus the
    30s wait, once per archive across a whole backfill run.

    Read through ``getattr`` so a provider that raises a plain exception
    (timeout, subprocess error) stays retriable, which is the safe default.
    """
    return getattr(exc, "transient", None) is False


@dataclass(frozen=True)
class RetryOutcome:
    """What one guarded model call actually did.

    The three extraction paths used to report a partial failure only through a
    log line and an empty string, so "the model was never asked because the
    account is over quota" and "the model answered nothing twice" were
    indistinguishable to a caller and to a test. ``gave_up`` names the reason
    instead.
    """

    output: str
    error: str
    #: How many times the call was actually made — 1 when a guard refused the
    #: retry, 2 when it ran and failed again.
    attempts: int
    #: "" when the call succeeded, else one of ``context-overflow``, ``terminal``, ``failed-twice``.
    gave_up: str = ""


async def call_with_retry(
    call: Callable[[], Awaitable[str]],
    *,
    label: str,
    check_context_overflow: bool = True,
    budget_applies: bool = True,
) -> RetryOutcome:
    """Run ``call``; on a transient failure wait 30s and run it once more.

    The one place the insights retry policy lives. It previously existed three
    times — for the JSONL input, for the rendered-archive input, and inline in
    the backfill worker — and the copies had drifted: only the JSONL one checked
    for a context overflow. The drift is now explicit in the keyword flags
    rather than implicit in which copy you were reading.

    Two failures are never retried, because an identical second request fails
    the same way and costs another slow call plus the 30s wait:

    * the input still exceeds the model's context window (the payload was
      already trimmed to the configured budget before the first call),
    * the provider classified the rejection as non-transient — auth, quota,
      usage limit, bad model.

    ``label`` prefixes the log lines so a reader can still tell the three paths
    apart ("Insights model call", "Insights text call", "Text fallback insights
    call").
    """
    try:
        return RetryOutcome(output=await call(), error="", attempts=1)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).strip() or type(exc).__name__
        if check_context_overflow and is_context_overflow(exc):
            # Only the JSONL path fits its payload to the input budget, so only
            # that message names it. Both end at the remedy that always applies.
            if budget_applies:
                logger.error(
                    "%s input still exceeds the model's context window (%s); "
                    "not retrying. The transcript was already trimmed to %d "
                    "chars; pick a model with a larger window.",
                    label,
                    exc,
                    _DEFAULT_MAX_INPUT_CHARS,
                )
            else:
                logger.error(
                    "%s input still exceeds the model's context window (%s); "
                    "not retrying. This path sends the rendered archive whole, "
                    "so pick a model with a larger window.",
                    label,
                    exc,
                )
            return RetryOutcome("", detail, 1, "context-overflow")
        if is_terminal_failure(exc):
            # Quota / auth / bad-model. No traceback: this is an account or
            # settings condition, not a code fault, and the detail already
            # says which.
            logger.error("%s rejected terminally (%s); not retrying", label, exc)
            return RetryOutcome("", detail, 1, "terminal")
        logger.info("%s failed (%s); retrying in %ds", label, exc, _RETRY_DELAY_S)

    await asyncio.sleep(_RETRY_DELAY_S)
    try:
        return RetryOutcome(output=await call(), error="", attempts=2)
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s failed twice; skipping", label)
        return RetryOutcome("", str(exc).strip() or type(exc).__name__, 2, "failed-twice")


# Rules shared verbatim by both extraction prompts (JSONL and text mode).
# Stated once so the two modes cannot drift apart — the same reason the
# curation contract was collapsed into one skill file.

# The Markdown output contract, kept apart from the grounding rules above.

def filter_session_jsonl(
    workspace_root: Path,
    session_id: str,
    *,
    agent_root: Path | None = None,
) -> str | None:
    """Read and pre-filter a Claude Code session JSONL into a compact string.

    Returns None when the file doesn't exist or can't be parsed.
    The returned string is line-oriented JSON (one filtered record per
    line) ready to be passed to the model as the user prompt body.

    Filter rules:
    - Drop sidechain entries, system pings, hook outputs, summary records.
    - Keep user messages, assistant text/thinking blocks in full.
    - Keep Edit/Write/Bash/Task tool_use and matching tool_result in full.
    - Keep any tool_result with is_error=true in full.
    - Truncate Read/Glob/Grep/WebFetch tool_result bodies to a head + size.
    - Annotate every kept message with a sequential ``idx`` for citation.

    ``agent_root`` is the per-workspace agent root whose session directory to
    read; it defaults to ``workspace_root`` so callers that supply nothing
    keep today's behaviour.
    """
    if not session_id:
        return None
    root = agent_root if agent_root is not None else workspace_root
    path = _claude_projects_dir(root) / f"{session_id}.jsonl"
    if not path.exists():
        return None

    truncate_tool_use_ids: set[str] = set()
    out_lines: list[str] = []
    idx = 0
    try:
        with path.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("isSidechain"):
                    continue
                otype = obj.get("type")
                if otype not in {"user", "assistant"}:
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue

                content = msg.get("content")
                kept_blocks = _filter_blocks(content, truncate_tool_use_ids)
                if not kept_blocks:
                    continue

                idx += 1
                record = {
                    "idx": idx,
                    "type": otype,
                    "ts": obj.get("timestamp", ""),
                    "content": kept_blocks,
                }
                # An unattended (schedule/automation) turn carries the capsule
                # marker in its injected context. Flag it so the extraction
                # model can tell machinery from a real user turn.
                if otype == "user" and _UNATTENDED_MARKER in _stringify_content(content):
                    record["unattended"] = True
                out_lines.append(json.dumps(record, ensure_ascii=False))
    except OSError:
        logger.exception("Could not read session JSONL at %s", path)
        return None

    if not out_lines:
        return None
    return "\n".join(out_lines)


def _filter_blocks(content: object, truncate_tool_use_ids: set[str]) -> list[object]:
    """Keep durable blocks; truncate read-only tool_result bodies."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    if not isinstance(content, list):
        return []

    kept: list[object] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                kept.append({"type": "text", "text": text})
        elif btype == "thinking":
            thinking = block.get("thinking", "")
            if isinstance(thinking, str) and thinking.strip():
                kept.append({"type": "thinking", "text": thinking})
        elif btype == "tool_use":
            name = block.get("name", "")
            tool_id = block.get("id", "")
            tool_input = block.get("input", {})
            kept.append({
                "type": "tool_use",
                "name": name,
                "id": tool_id,
                "input": _summarise_tool_input(name, tool_input),
            })
            if isinstance(tool_id, str) and name in _TRUNCATE_TOOLS:
                truncate_tool_use_ids.add(tool_id)
        elif btype == "tool_result":
            kept.append(_filter_tool_result(block, truncate_tool_use_ids))
    return kept


def _summarise_tool_input(name: str, tool_input: object) -> object:
    """Keep tool inputs small. Edit/Write keep full content; Read keeps path only."""
    if not isinstance(tool_input, dict):
        return tool_input
    if name in _KEEP_FULL_TOOLS:
        return tool_input
    if name in _TRUNCATE_TOOLS:
        keep_keys = ("file_path", "path", "pattern", "url", "query")
        return {k: tool_input[k] for k in keep_keys if k in tool_input}
    return tool_input


def _filter_tool_result(
    block: dict, truncate_tool_use_ids: set[str]
) -> dict:
    """Truncate read-only tool_result content; keep errors and writes in full."""
    tool_use_id = block.get("tool_use_id", "")
    is_error = bool(block.get("is_error"))
    raw_content = block.get("content")

    if is_error or tool_use_id not in truncate_tool_use_ids:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": is_error,
            "content": _stringify_content(raw_content),
        }

    text = _stringify_content(raw_content)
    full_len = len(text)
    truncated = text[:_READ_TOOL_TRUNCATE_CHARS]
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": False,
        "content": f"{truncated}…[truncated, total={full_len} chars]",
    }


def _stringify_content(content: object) -> str:
    """Flatten tool_result content into a single string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


async def run_archive_pipeline(
    job: Any,
    inputs: dict[str, Any],
    *,
    stages: list[str] | None = None,
) -> Any:
    """Execute the requested pipeline stages, recording each on the manifest.

    One stage: the session trajectory. Everything that used to write to the
    vault here — the insights section, the project-doc fold, the memory
    proposals — moved to the memory pass, a chat of the app's own that the
    archive enqueues instead (see ``ciao/web/memory_pass.py``). The trajectory
    stayed because it is the one output that must exist while the raw session
    JSONL is still on disk: archive time is the only moment it can be built.

    ``stages`` defaults to the job's resumable set. The stage re-derives its own
    prior completion (it overwrites its own file), so running it twice is safe.
    A failure is recorded ``failed`` and never crashes the caller.
    """
    from ciao.archive_jobs import FAILED, RUNNING, SKIPPED, SUCCEEDED, TOMBSTONED

    if job.tombstoned or job.state == TOMBSTONED:
        logger.info(
            "Archive pipeline skipped for %s: job tombstoned", job.archive_path
        )
        return job

    archive_path: Path = inputs["archive_path"]
    chat_id = job.chat_id
    order = list(stages) if stages is not None else job.resumable()
    if not order:
        return job

    if not archive_path.exists():
        # A resume whose archive is gone is blocked, not failed: the file may
        # come back (a restore) and a retry should then succeed.
        for name in order:
            job.block(name, "archive file is missing")
        job.save()
        return job

    job.started = True
    config = inputs["config"]
    filtered_jsonl = str(inputs.get("filtered_jsonl") or "")
    session_id = str(inputs.get("session_id") or "")
    workspace_root = inputs.get("workspace_root")
    trajectory_meta = dict(inputs.get("trajectory_meta") or {})
    trajectories_enabled = bool(inputs.get("trajectories_enabled", True)) and bool(
        getattr(config, "trajectories_enabled", True)
    )

    for name in order:
        if job.tombstoned:
            return job
        if job.status_of(name) in (SUCCEEDED, SKIPPED):
            continue
        if name == "trajectory" and not (
            trajectories_enabled and session_id and filtered_jsonl
        ):
            job.mark(name, SKIPPED, "no session input or trajectories disabled")
            job.save()
            continue

        try:
            job.mark(name, RUNNING)
            if not job.save():
                # A stage that cannot record that it started must not run:
                # otherwise the trajectory write lands while the durable
                # manifest still says pending, and a crash has startup replay
                # work that already happened. The handler below marks the stage
                # failed, which is retryable.
                raise RuntimeError("could not persist the archive job manifest")

            if name == "trajectory":
                from ciao.trajectory_builder import build_and_persist_trajectory

                trajectory_errors: list[str] = []
                with job_runs.track_sync(
                    "trajectory", "Trajectory capture",
                    extra={"session_id": session_id, "chat_id": chat_id},
                ) as run:
                    path = build_and_persist_trajectory(
                        session_id=session_id,
                        filtered_jsonl=filtered_jsonl,
                        archive_path=archive_path,
                        context=trajectory_meta.get("context", ""),
                        project_id=trajectory_meta.get("project_id", ""),
                        chat_id=trajectory_meta.get("chat_id", ""),
                        task_summary=trajectory_meta.get("task_summary", ""),
                        workspace=trajectory_meta.get("workspace", ""),
                        workspace_root=workspace_root,
                        error_out=trajectory_errors,
                    )
                    if path:
                        run.extra["path"] = str(path)
                    elif trajectory_errors:
                        run.status = "error"
                        run.error = trajectory_errors[-1]
                    else:
                        run.skip("empty session / no trajectory written")
                if trajectory_errors:
                    # Eligibility already proved the input was non-empty, so a
                    # None here is a parse/persist failure, not "nothing to do".
                    raise RuntimeError(trajectory_errors[-1])
                job.mark(name, SUCCEEDED)
                job.save()
                continue

        except Exception as exc:  # noqa: BLE001 — never crash the caller
            logger.exception(
                "Archive pipeline stage %s failed for %s", name, archive_path
            )
            job.mark(name, FAILED, f"{type(exc).__name__}: {exc}"[:400])
            job.save()
            continue

    job.save()
    return job


def locate_insights_section(text: str) -> tuple[int, int] | None:
    """Locate the real appended Session-insights section of an archive.

    Returns ``(section_start, body_start)`` — the offset where the section
    (stamp included) begins, and the offset just past the header line — or
    ``None`` when the archive carries no appended section.

    A plain substring match is not enough: chats that *discuss* insights
    (nightly curation, meta work on the pipeline) quote the header verbatim
    inside their transcript, which made the old check skip extraction for
    those archives and let the proposal parser re-ingest already-reviewed
    bullets. Resolution order:

    * A stamped section is authoritative — but only a stamp immediately
      followed by the header line, the exact shape that stage wrote, with no
      transcript structure after it. The section is always
      the last thing in the file, so anything transcript-shaped after the
      header (a turn heading, a trailer, or a line-start ``` — rendered
      archives fence quoted text, so a quoted stamp is followed by at least
      its closing fence) proves the stamp is quoted content. The check looks
      only *forward*: fence *parity* over the prefix would be flipped by a
      single unbalanced ``` inside any earlier turn's verbatim text — common
      in chats that paste partial code blocks — and would hide the real
      section, re-triggering extraction on every pass.
    * Otherwise take the last line-anchored header, under the same
      forward-looking rule.
    """
    stamped_matches = list(_STAMPED_MARKER_RE.finditer(text))
    if stamped_matches:
        # The real stamp is always the last stamp+header pair; if this one is
        # followed by transcript structure it is quoted content, and every
        # earlier pair sits even deeper in the transcript.
        stamped = stamped_matches[-1]
        if _is_appended_tail(text, stamped.end()):
            return stamped.start(), stamped.end()
        return None
    matches = list(_MARKER_LINE_RE.finditer(text))
    if matches and _is_appended_tail(text, matches[-1].end()):
        return matches[-1].start(), matches[-1].end()
    return None


def _is_appended_tail(text: str, idx: int) -> bool:
    """True when everything after *idx* looks like an appended insights body.

    Transcript structure after a marker — a turn heading, the Subagents
    block, a Usage/Quota trailer, or a line-start ``` fence — proves the
    marker is quoted transcript content, not the section the pipeline
    appended at end of file. (The appended body's own snippet fences are
    indented and never start a line.)
    """
    return not _TURN_HEADING_RE.search(text, idx) and not _FENCE_LINE_RE.search(
        text, idx
    )


def _has_insights_section(path: Path) -> bool:
    try:
        return locate_insights_section(path.read_text(encoding="utf-8")) is not None
    except OSError:
        return False

