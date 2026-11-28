"""The proposal file: what an agent writes when it is asked for a proposal.

A skill edit, an upstream issue, a guide edit or a note retirement is the
result of a request, not a queue entry. The agent writes one Markdown file,
``Workspace/Proposals/<slug>.md``, with YAML front matter and a body. A person
files the issue or applies the edit from that file, or asks the agent to.

:func:`check` is the whole validator: it reports every problem at once, in words
the agent can act on, and never touches the file. It does not judge whether the
proposal is a good one.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

import yaml

from ciao.redact import redact_text

PROPOSAL_TYPES = ("skill-edit", "upstream-issue", "guide-edit", "note-retire")
PROPOSAL_STATUSES = ("draft", "ready", "applied", "rejected")
PROPOSALS_DIR = "Proposals"

REQUIRED_FIELDS = (
    "type",
    "target",
    "title",
    "summary",
    "rationale",
    "evidence",
    "proposed_change",
    "created",
    "status",
)

MAX_FILE_BYTES = 64 * 1024
MAX_TITLE_CHARS = 120
MAX_SUMMARY_CHARS = 400
MAX_EVIDENCE_ITEMS = 20

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*(?:\n|\Z)(.*)\Z", re.DOTALL)


def _relative_path_problem(value: str, *, suffix: str = "") -> str | None:
    if value.startswith(("/", "~")) or ".." in value.replace("\\", "/").split("/"):
        return "must be a relative path without `..`"
    if suffix and not value.endswith(suffix):
        return f"must end in `{suffix}`"
    return None


def _target_problem(kind: str, target: str) -> str | None:
    if kind == "skill-edit" and not _SKILL_NAME.fullmatch(target):
        return "`target` for a skill-edit is the skill's name, for example `ciao-memory`"
    if kind == "upstream-issue" and not _REPO.fullmatch(target):
        return "`target` for an upstream-issue is the repository as `owner/name`"
    if kind == "guide-edit":
        why = _relative_path_problem(target)
        if why:
            return f"`target` for a guide-edit {why} (for example `AGENTS.md`)"
    if kind == "note-retire":
        why = _relative_path_problem(target, suffix=".md")
        if why:
            return f"`target` for a note-retire is the note's vault path; it {why}"
    return None


def _blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def read_front_matter(text: str) -> tuple[dict[str, Any], str] | None:
    """The front matter as a mapping and the body after it; None when there is none to read."""
    match = _FRONT_MATTER.match(text)
    if match is None:
        return None
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded, match.group(2)


def check(text: str, *, filename: str, parent: str | None = None) -> list[str]:
    """Every problem with one proposal file; an empty list means it is valid."""
    problems: list[str] = []
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        return [f"the file is over {MAX_FILE_BYTES // 1024} KB; cut it down"]
    if not _SLUG.fullmatch(filename):
        problems.append(
            f"file name `{filename}` must be a lowercase slug such as `tighten-memory-skill.md`"
        )
    if parent is not None and parent != PROPOSALS_DIR:
        problems.append(f"the file belongs in `Workspace/{PROPOSALS_DIR}/`, not in `{parent}/`")

    match = _FRONT_MATTER.match(text)
    if match is None:
        problems.append("the file must start with YAML front matter between two `---` lines")
        return problems
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        problems.append("the front matter is not valid YAML; quote values that contain a colon")
        return problems
    if not isinstance(loaded, dict):
        problems.append("the front matter must be a list of `field: value` lines")
        return problems
    fields: dict[str, Any] = loaded

    missing = [name for name in REQUIRED_FIELDS if _blank(fields.get(name))]
    if missing:
        problems.append("missing or empty front matter fields: " + ", ".join(missing))
    unknown = sorted(str(name) for name in fields if name not in REQUIRED_FIELDS)
    if unknown:
        problems.append("unknown front matter fields: " + ", ".join(unknown))

    kind = fields.get("type")
    if not _blank(kind) and kind not in PROPOSAL_TYPES:
        problems.append("`type` must be one of " + ", ".join(PROPOSAL_TYPES))
    status = fields.get("status")
    if not _blank(status) and status not in PROPOSAL_STATUSES:
        problems.append("`status` must be one of " + ", ".join(PROPOSAL_STATUSES))

    target = fields.get("target")
    if isinstance(target, str) and target.strip():
        if kind in PROPOSAL_TYPES:
            why = _target_problem(str(kind), target.strip())
            if why:
                problems.append(why)
    elif not _blank(target):
        problems.append("`target` must be text")

    for name, limit in (("title", MAX_TITLE_CHARS), ("summary", MAX_SUMMARY_CHARS)):
        value = fields.get(name)
        if isinstance(value, str):
            if len(" ".join(value.split())) > limit:
                problems.append(f"`{name}` is over {limit} characters; shorten it")
        elif not _blank(value):
            problems.append(f"`{name}` must be text")
    for name in ("rationale", "proposed_change"):
        value = fields.get(name)
        if not _blank(value) and not isinstance(value, str):
            problems.append(f"`{name}` must be text")

    created = fields.get("created")
    if not _blank(created):
        if isinstance(created, _dt.datetime):
            problems.append("`created` is a date as YYYY-MM-DD, without a time")
        elif not isinstance(created, _dt.date):
            try:
                _dt.date.fromisoformat(str(created))
            except ValueError:
                problems.append("`created` must be a date as YYYY-MM-DD")

    evidence = fields.get("evidence")
    if not _blank(evidence):
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) and item.strip() for item in evidence
        ):
            problems.append(
                "`evidence` must be a list of links, note paths or chat ids, one per `-` line"
            )
        elif len(evidence) > MAX_EVIDENCE_ITEMS:
            problems.append(f"`evidence` lists more than {MAX_EVIDENCE_ITEMS} items; keep the best")

    if not match.group(2).strip():
        problems.append("the body is empty; explain the proposal in Markdown below the front matter")

    leaking = [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if redact_text(line) != line
    ]
    if leaking:
        shown = ", ".join(str(n) for n in leaking[:5])
        problems.append(
            f"credential-shaped text on line(s) {shown}: name the credential, never its value"
        )
    return problems
