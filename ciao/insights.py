"""Post-archive session insights extraction.

When a chat is archived, the user/assistant text turns are rendered to
``memory-vault/Logs/Chats/<context>/claude/<file>.md`` by
``TranscriptStore.archive_session``. That renderer drops everything that
isn't plain text: tool_use, tool_result, thinking blocks, errors, retries.

This module mines the raw Claude Code session JSONL (at
``~/.claude/projects/-home-ubuntu-ciao/<session-id>.jsonl``) for the
durable signal those layers contain, runs it through a fast cheap model
(DeepSeek Flash by default), and appends a ``## Session insights``
section to the archived markdown. Downstream consumers (memory curation,
work daily log, weekly review) read that section instead of mining the
JSONL themselves.

The flow is split in two phases for safety:

* :func:`filter_session_jsonl` runs synchronously inside ``archive_chat``
  before ``delete_sdk_session_blob`` removes the JSONL from disk. It
  reads the file, drops noise, truncates large read-only tool_result
  bodies, and returns a much smaller string ready for the model.
* :func:`run_archive_pipeline` runs asynchronously via
  ``asyncio.create_task`` from the archive route handler. It is the manifest
  runner, and it drives the trajectory stage.
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
# Written by _append_section immediately before the header so the real
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
_KNOWN_CONTEXT_MAX_CHARS = 6000
_KNOWN_CONTEXT_MAX_NAMES = 120


def _known_context_block(
    guide_path: Path | None, vault_root: Path | None, transcript: str = ""
) -> str:
    """Workspace context the extractor should know, fetched by code.

    Fact-augmented extraction (see docs/MEMORY_DESIGN.md): the model gets the
    current always-loaded memory entries and a roster of known people and
    projects — so it can omit already-covered facts, emit changed ones, and
    only call an entity "new" when it is absent from the roster — without
    getting tools. Retrieval stays deterministic and the model stays
    sandboxed. Best-effort: any failure returns what was gathered so far, and
    an empty result means the prompt simply carries no context section.
    """
    parts: list[str] = []
    try:
        if guide_path is not None and guide_path.exists():
            from ciao.memory_tool import read_region

            for region in ("memory", "profile"):
                entries, diags = read_region(guide_path, region)
                if diags or not entries:
                    continue
                parts.append(f"Current `ciao:{region}` entries:")
                parts.extend(f"- {entry}" for entry in entries)
    except Exception:  # noqa: BLE001 — context is optional
        logger.exception("Known-context: could not read regions")
    projects: dict[str, Path] = {}
    people: dict[str, str] = {}
    try:
        if vault_root is not None and vault_root.exists():
            # The same roster `memory_proposals` resolves `[project: <name>]`
            # and `[people: <Name>]` against, so the prompt never offers a
            # name code cannot route (a folder with no doc, `general`).
            from ciao.memory_proposals import known_entities, project_name

            projects, people = known_entities(vault_root)
            names = sorted(people.values())[:_KNOWN_CONTEXT_MAX_NAMES]
            if names:
                parts.append("Known people: " + ", ".join(names))
            names = sorted({project_name(doc) for doc in projects.values()})
            if names:
                parts.append("Known projects: " + ", ".join(names[:_KNOWN_CONTEXT_MAX_NAMES]))
    except Exception:  # noqa: BLE001 — context is optional
        logger.exception("Known-context: could not build entity roster")
    if not parts:
        return ""
    block = (
        "## Known context (fetched from the workspace, NOT transcript content)\n"
        + "\n".join(parts)
    )
    if len(block) > _KNOWN_CONTEXT_MAX_CHARS:
        # Cut at a line boundary: a mid-entry or mid-name cut would present a
        # corrupted entry (or half a person's name) as reference data the
        # prompt tells the model to trust.
        cut = block.rfind("\n", 0, _KNOWN_CONTEXT_MAX_CHARS)
        block = block[:_KNOWN_CONTEXT_MAX_CHARS] if cut <= 0 else block[:cut]
    notes = ""
    if transcript and vault_root is not None:
        try:
            notes = _entity_notes_block(vault_root, transcript, projects, people)
        except Exception:  # noqa: BLE001 — context is optional
            logger.exception("Known-context: could not excerpt entity notes")
    return block + "\n\n" + notes


# Excerpts of the notes this session's entities already have. Capped apart
# from the roster above so a long roster can never crowd them out, and small:
# enough for the model to see what a note already says, not the note itself.
_ENTITY_NOTES_MAX = 8
_ENTITY_NOTE_MAX_LINES = 6
_ENTITY_NOTE_LINE_CHARS = 240
_ENTITY_NOTES_MAX_CHARS = 8000


def _archive_body_for_mentions(archive_path: Path) -> str:
    """The rendered archive text-mode extraction reads, for entity mentions."""
    try:
        return archive_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _note_excerpt(path: Path) -> str:
    """A note's ``description:`` plus its newest body lines, each clipped."""
    from ciao.vault_index import FENCED_CODE_RE, FRONTMATTER_RE, _parse_frontmatter

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # FRONTMATTER_RE wants LF endings and a newline after the closing fence.
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    description = str(_parse_frontmatter(text).get("description") or "").strip()
    match = FRONTMATTER_RE.match(text)
    body = FENCED_CODE_RE.sub("", text[match.end():] if match else text)
    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "<!--", "|"))
    ][-_ENTITY_NOTE_MAX_LINES:]
    out = [f"description: {' '.join(description.split())}"] if description else []
    for line in lines:
        if len(line) > _ENTITY_NOTE_LINE_CHARS:
            line = line[: _ENTITY_NOTE_LINE_CHARS - 1].rstrip() + "…"
        out.append(line)
    return "\n".join(out)


def _entity_notes_block(
    vault_root: Path,
    transcript: str,
    projects: dict[str, Path],
    people: dict[str, str],
) -> str:
    """Excerpts of the known people/project notes the transcript mentions.

    The roster alone told the model which names exist but not what their
    notes say, so it could not tell a new fact from a restated one, and it
    re-proposed "Project: Wedding - civil wedding + party" for a project whose
    doc says exactly that. Each excerpt is headed by the destination tag that
    routes to it, so the tag the model writes is the one code resolves.
    """
    from ciao.memory_proposals import entity_mention_counts, project_name

    # A person and a project may share a name; a mention of it shows both.
    notes: dict[str, list[tuple[str, Path]]] = {}
    for key, doc in projects.items():
        notes.setdefault(key, []).append((f"[project: {project_name(doc)}]", doc))
    for key, stem in people.items():
        notes.setdefault(key, []).append(
            (f"[people: {stem}]", vault_root / "People" / f"{stem}.md")
        )
    counts = entity_mention_counts(transcript, notes)
    ranked = [
        note
        for key in sorted(counts, key=lambda key: -counts[key])
        for note in notes[key]
    ]
    sections: list[str] = []
    total = 0
    for tag, path in ranked[:_ENTITY_NOTES_MAX]:
        excerpt = _note_excerpt(path)
        if not excerpt:
            continue
        section = f"{tag}\n{excerpt}"
        if total + len(section) > _ENTITY_NOTES_MAX_CHARS:
            break
        sections.append(section)
        total += len(section) + 2
    if not sections:
        return ""
    return (
        "## Known notes for entities this session mentions "
        "(fetched from the workspace, NOT transcript content)\n"
        + "\n\n".join(sections)
        + "\n\n"
    )


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


def _fit_transcript(filtered_jsonl: str, *, reserve: int = 0) -> tuple[str, int]:
    """Trim a transcript to the input budget, dropping oldest lines first.

    Returns ``(payload, dropped_line_count)``. Newest turns are kept because
    they carry the session's conclusions; the surviving lines keep their
    original ``idx`` values, so the citations the prompt demands stay valid.

    ``reserve`` is subtracted from the budget for prompt text prepended after
    fitting (the known-context block) — the oversized-input rejection is
    deliberately not retried, so the first call must already be within budget.
    """
    budget = max(0, _DEFAULT_MAX_INPUT_CHARS - reserve)
    if len(filtered_jsonl) <= budget:
        return filtered_jsonl, 0
    lines = filtered_jsonl.splitlines()
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        total += len(line) + 1
        if total > budget:
            break
        kept.append(line)
    kept.reverse()
    return "\n".join(kept), len(lines) - len(kept)


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
_KNOWN_CONTEXT_RULE = """\
- The user prompt may open with a "Known context" section fetched from the
  workspace: current always-loaded memory entries and a roster of known
  people and projects. It is reference data, not transcript content. A fact
  already covered by a current memory entry must be OMITTED; when the
  transcript shows a known fact CHANGED, emit the updated fact. A person or
  project in the roster is never a New entity — only names absent from the
  roster qualify.
- A "Known notes" section, when present, excerpts the existing note of each
  known person or project this session mentions, headed by the tag that
  files into it. A fact that note already states must be OMITTED. A new or
  changed fact about that entity goes in whichever section fits, tagged with
  that heading's tag exactly as written.
"""

_FINAL_STATEMENT_RULE = """\
- Be terse. One line per item where possible. Every bullet is a final
  statement: never narrate reconsideration, hedging, or self-correction
  inside a bullet — resolve it first, then write only the surviving fact.
"""


_INSIGHTS_RULES = """\
You are extracting durable signal from a Claude Code session transcript.
The user is the workspace owner. Output Markdown with the exact section headers below.
Omit a section entirely if empty - do NOT write "none" or "n/a".
Cite the message index `[idx=N]` for every claim. Indices start at 1;
never cite `[idx=0]`. Do not invent facts.
Do not summarise the conversation - that is already saved.

Rules:
- Emit ONLY durable, cross-session facts. A fact is worth keeping only if it
  will matter in a future session: a standing preference, a reusable lesson, a
  real error pattern, a recurring tool, a new person/project. Omit a section
  entirely rather than fill it with session-local noise — a one-off choice
  about this one repo, a single loop, or a phrasing pushback that was only
  about this session has no place here.
- If this is a scheduled maintenance session (memory curation, hygiene
  audits, skill evolution), never extract the session's own operating
  instructions, prompt rules, or memory-system procedures as facts — they
  are machinery, not knowledge about the user.
- A user message flagged `"unattended": true` is an automation turn (a
  schedule or routine fired it), not the user typing. Never extract a fact
  from an unattended turn or from the assistant work it triggered. Only
  extract facts from turns the user actually typed. A real user turn in an
  otherwise-automated session is still fair game.
""" + _KNOWN_CONTEXT_RULE + """\
- When a fact is only true from or until a date, append `[as-of: YYYY-MM-DD]`
  or `[expires: YYYY-MM-DD]` to the bullet, before the citation and
  destination tag. Never invent a date the transcript does not support.
- End every bullet with exactly one destination tag, after the citation:
  - [memory] - true regardless of which project is open: a standing
    preference, an environment fact, a cross-project lesson.
  - [profile] - who the user is: identity, role, communication style.
  - [project] - true only within this chat's own project/repo: its
    decisions, constraints, status. When unsure whether a fact is
    project-scoped or global, use [review] instead of guessing.
  - [project: <name>] - true only within a DIFFERENT project listed under
    "Known projects"; use the name exactly as listed. Never invent one.
  - [people: <Name>] - a durable fact about a person; for someone under
    "Known people", use the name exactly as listed.
  - [learnings] - reusable how-to knowledge that spans projects.
  - [review] - durable, but you are not sure where it belongs.
- Skip routine successful tool calls.
- Skip anything obvious from user/assistant text alone.
- "Errors" = tool/model/system failure, not just things the user disliked.
- "User corrections" = a correction that implies a preference the user wants to
  hold in future sessions. Drop corrections that only fixed this session's
  output. Append the "Durable rule:" sentence ONLY when the user stated a
  present-tense standing rule; if the correction has no durable rule, do NOT
  write the bullet at all.
- "New entities" = people, phrases, places, or products mentioned for the first
  time that the user will keep dealing with — not generic nouns, not one-off
  references to something in this transcript.
- "Decisions" = choices that set a precedent for future sessions ("chose X over
  Y, and we should keep doing X"). Drop one-off picks about this transcript.
  Decisions is not a changelog: never list what the session fixed, added,
  deleted or committed, and never restate an edit already listed under
  "Vault changes" — that file already holds it.
- When citing a vault link, use a relative Markdown link with the path from the
  vault root: [Mo](./People/Mo.md). Do NOT use [[bracketed-wikilinks]] and do NOT wrap the link in backticks, quotes, or other formatting.
""" + _FINAL_STATEMENT_RULE

# The Markdown output contract, kept apart from the grounding rules above.
_INSIGHTS_SECTION_SCHEMA = """
## Errors
- <what failed> -> <how it was resolved, or "unresolved">. Only a failure whose fix is worth remembering. [idx=N] <tag>

## User corrections
- <the standing rule that holds in future sessions>, phrased as present-tense state. Durable rule: <the same rule, present tense>. Never "User said: <quote> -> assistant did <x>" alone. [idx=N] <tag>

## New entities
- <type>: <name> - <one-line context>. Only recurring names. [idx=N] <tag>

## Decisions
- Chose <X> over <Y> because <reason>; this governs future sessions. Only precedent-setting choices. [idx=N] <tag>

## Reusable snippets
- <one-line description>:
  ```<lang>
  <command/query/config>
  ```

## Open loops
- <thing left undone, with any deadline or condition>. [idx=N] <tag>

## Vault changes
- <path> - <one-line summary of edit>. [idx=N]
"""

_INSIGHTS_SYSTEM_PROMPT = _INSIGHTS_RULES + _INSIGHTS_SECTION_SCHEMA

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
      followed by the header line, the exact shape :func:`_append_section`
      writes, with no transcript structure after it. The section is always
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


def _indent_body_fences(text: str) -> str:
    """Indent any line-start ``` in the body we are about to append.

    `_is_appended_tail` treats a line-start fence after the stamp as proof the
    stamp is quoted transcript content, and its docstring asserts that the
    appended body's own fences are always indented. The prompt's "## Reusable
    snippets" template does indent them - but the model does not reliably
    preserve that, and one unindented fence made the whole section invisible:
    `locate_insights_section` returned None, so memory proposals filed nothing
    and every backfill run appended ANOTHER copy of the section.

    Indenting here makes that invariant true by construction instead of by
    convention. Two spaces is what the template already uses, and is still a
    fence to any CommonMark renderer (up to three spaces of indent).
    """
    return "\n".join(
        f"  {line}" if line.startswith("```") else line for line in text.split("\n")
    )


def _format_section(body: str) -> str:
    """The exact insights section ``_append_section`` writes, or '' for empty."""
    text = _indent_body_fences(body.strip())
    if not text:
        return ""
    return f"{_INSIGHTS_STAMP}\n{_INSIGHTS_HEADER}\n\n{text}\n"


def _append_section(path: Path, body: str) -> str:
    """Append the insights section; return the exact section text written.

    The returned string is the section from its stamp onward — exactly what
    ``locate_insights_section`` points at — so a caller can record its hash as
    crash-recovery evidence: a resume authenticates the on-disk section against
    it instead of trusting any tail that follows a matching prefix.
    """
    section = _format_section(body)
    if not section:
        return ""
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n\n{section}")
    return section


async def _run_model_with_retry(
    *,
    filtered_jsonl: str,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
    context_block: str = "",
) -> tuple[str, str]:
    """Call the model; on a transient failure, wait 30s and retry once.

    An oversized-input rejection is not retried: the payload is already
    trimmed to the configured budget before the first call, so a second
    identical request would fail the same way.
    """
    # The context block is prepended AFTER fitting, so its length is reserved
    # here — otherwise the final prompt overshoots the budget the no-retry
    # oversized-input policy relies on.
    reserve = len(context_block)
    payload, dropped = _fit_transcript(filtered_jsonl, reserve=reserve)
    budget = max(0, _DEFAULT_MAX_INPUT_CHARS - reserve)
    if dropped:
        logger.info(
            "Insights transcript over the %d-char budget; dropped %d oldest line(s)",
            budget,
            dropped,
        )

    async def call() -> str:
        if provider == "claude":
            return await _call_model(payload, model, context_block=context_block)
        return await _call_model(
            payload, model, provider=provider, cwd=cwd, context_block=context_block
        )

    outcome = await call_with_retry(call, label="Insights model call")
    return outcome.output, outcome.error


def _text_user_prompt(body: str, context_block: str = "") -> str:
    """Prompt for text-mode extraction (archive markdown, no JSONL indices)."""
    return (
        context_block
        + "Below is a rendered Markdown chat transcript. Tool calls, errors, "
        "and thinking blocks are not preserved - only user/assistant text. "
        "Extract durable signal per the system prompt's section schema.\n\n"
        f"{body}"
    )


async def _call_text_model(
    body: str,
    model: str,
    *,
    provider: str = "claude",
    cwd: Path | None = None,
    context_block: str = "",
) -> str:
    """Run text-mode extraction for ``model`` on a rendered archive body."""
    from ciao.providers.oneshot import run_oneshot

    return await run_oneshot(
        _text_user_prompt(body, context_block),
        system_prompt=_TEXT_MODE_SYSTEM_PROMPT,
        model=model,
        timeout_s=_DEFAULT_TIMEOUT_S,
        cwd=cwd,
        provider=provider,
    )


async def _run_text_model_with_retry(
    *,
    archive_path: Path,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
    context_block: str = "",
) -> tuple[str, str]:
    """Run text-mode extraction on ``archive_path``; retry once on failure.

    Mirrors :func:`_run_model_with_retry` for the rendered-archive input, so a
    failed archive can be retried even after its raw JSONL is reclaimed.
    """
    try:
        body = archive_path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("Could not read archive %s for insights retry", archive_path)
        return "", "archive unreadable"

    async def call() -> str:
        return await _call_text_model(
            body, model, provider=provider, cwd=cwd, context_block=context_block
        )

    # An overflow is refused here for the same reason it is on the JSONL path:
    # the payload does not change between attempts, so the retry buys a second
    # slow call (the timeout budget is 600s) plus the 30s wait to reach the
    # identical rejection. This path used to retry it, which was an accident of
    # the policy existing in two copies rather than a decision.
    #
    # No fitting step, though, unlike the JSONL path — and measurement says it
    # does not need one. Text mode's input is the *rendered* archive, which is
    # the stripped rendering (no tool_use, tool_result or thinking blocks); the
    # 320k-char budget exists for raw JSONL, observed at 131k-262k tokens
    # against a 126k-token window. Across 1568 real archives the rendered form
    # runs ~2.6k tokens at the median and ~23k at p99, with exactly one
    # outlier (134k tokens) able to overflow a 126k-token model at all.
    # Truncating would be a general mechanism for a single archive.
    outcome = await call_with_retry(
        call, label="Insights text call", budget_applies=False
    )
    return outcome.output, outcome.error


async def _call_model(
    filtered_jsonl: str,
    model: str,
    *,
    provider: str = "claude",
    cwd: Path | None = None,
    context_block: str = "",
) -> str:
    from ciao.providers.oneshot import run_oneshot

    user_prompt = (
        context_block
        + "Below is a coding-agent session transcript as line-oriented JSON.\n"
        "Each line is one message with a numeric `idx` you must cite.\n"
        "Extract durable signal per the system prompt's section schema.\n\n"
        f"{filtered_jsonl}"
    )

    kwargs: dict[str, Any] = {
        "system_prompt": _INSIGHTS_SYSTEM_PROMPT,
        "model": model,
        "timeout_s": _DEFAULT_TIMEOUT_S,
    }
    if provider != "claude":
        kwargs.update({"provider": provider, "cwd": cwd})
    return await run_oneshot(user_prompt, **kwargs)


_TEXT_MODE_SYSTEM_PROMPT = """\
You are extracting durable signal from a Claude Code chat transcript.
The user is the workspace owner. The transcript is a rendered Markdown summary -
tool calls, tool errors, thinking blocks, and intermediate states are
NOT included, only the user/assistant text turns. Adjust accordingly:
sections like Errors, Reusable snippets, and Vault changes will often
be empty. Omit empty sections - do NOT write "none" or "n/a".

Cite by short paraphrase or quote (no `[idx=N]` indices in this mode).
Do not invent facts. Do not summarise the conversation - that is the
transcript itself.

Rules:
- Emit ONLY durable, cross-session facts: a standing preference, a reusable
  lesson, a real error pattern, a recurring name. Omit a section entirely
  rather than fill it with session-local noise — a one-off choice about this
  one repo, a single exchange, or a phrasing pushback that only fixed this
  session has no place here.
- If this is a scheduled maintenance session (memory curation, hygiene
  audits, skill evolution), never extract the session's own operating
  instructions, prompt rules, or memory-system procedures as facts — they
  are machinery, not knowledge about the user.
- A user message flagged `"unattended": true` is an automation turn (a
  schedule or routine fired it), not the user typing. Never extract a fact
  from an unattended turn or from the assistant work it triggered. Only
  extract facts from turns the user actually typed. A real user turn in an
  otherwise-automated session is still fair game.
""" + _KNOWN_CONTEXT_RULE + """\
- When a fact is only true from or until a date, append `[as-of: YYYY-MM-DD]`
  or `[expires: YYYY-MM-DD]` to the bullet, before the destination tag.
  Never invent a date the transcript does not support.
- End every bullet with exactly one destination tag:
  - [memory] - true regardless of which project is open: a standing
    preference, an environment fact, a cross-project lesson.
  - [profile] - who the user is: identity, role, communication style.
  - [project] - true only within this chat's own project/repo: its
    decisions, constraints, status. When unsure whether a fact is
    project-scoped or global, use [review] instead of guessing.
  - [project: <name>] - true only within a DIFFERENT project listed under
    "Known projects"; use the name exactly as listed. Never invent one.
  - [people: <Name>] - a durable fact about a person; for someone under
    "Known people", use the name exactly as listed.
  - [learnings] - reusable how-to knowledge that spans projects.
  - [review] - durable, but you are not sure where it belongs.
- "User corrections" = a correction that implies a preference the user wants to
  hold in future sessions. Drop corrections that only fixed this session's
  output. Append the "Durable rule:" sentence ONLY when the user stated a
  present-tense standing rule; if there is no durable rule, do NOT write the
  bullet at all.
- "New entities" = people/projects/places/products the user will keep dealing
  with, not one-off references in this transcript.
- "Decisions" = choices that set a precedent for future sessions; drop one-off
  picks about this transcript. Not a changelog: never list what the session
  fixed, added, deleted or committed, or restate an edit it already saved.
""" + _FINAL_STATEMENT_RULE + """
Your entire response must be Markdown using only the section headers below. Never
return JSON, a code-fenced transcript, session metadata, or a generic recap.

## User corrections
- <the standing rule that holds in future sessions>, phrased as present-tense state. Durable rule: <the same rule, present tense>. Never "User said: <quote> -> assistant did <x>" alone. <tag>

## New entities
- <type>: <name> - <one-line context>. Only recurring names. <tag>

## Decisions
- Chose <X> over <Y> because <reason>; this governs future sessions. Only precedent-setting choices. <tag>

## Open loops
- <thing left undone, with any deadline or condition>. <tag>

## Errors
- <if the transcript itself describes a failure resolution that's worth keeping> <tag>

## Reusable snippets
- <only if a fully formed command or query appears in the assistant text>
"""

